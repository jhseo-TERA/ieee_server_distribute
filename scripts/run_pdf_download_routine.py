# -*- coding: utf-8 -*-
"""Run bounded PDF acquisition with a shared daily budget and publisher cooldowns."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pymysql
from dotenv import load_dotenv
try:
    from .daily_pipeline_state import begin_pipeline, update_stage, POST_STAGES
    from .download_guard import bounded_download_limit, load_repair_ids
except ImportError:  # Direct execution from the scripts directory.
    from daily_pipeline_state import begin_pipeline, update_stage, POST_STAGES
    from download_guard import bounded_download_limit, load_repair_ids


ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = ROOT / "scripts"
STATE_DIR = ROOT / "logs" / "pdf_download_state"
STATE_PATH = STATE_DIR / "routine.json"
HISTORY_PATH = STATE_DIR / "routine_history.jsonl"
LOCK_PATH = STATE_DIR / "routine.lock"
PROVIDERS = (
    ("ieee", "download_pdfs.py"),
    ("jlt_ieee", "download_optica_pdfs.py"),
    ("optica", "download_optica_pdfs.py"),
    ("nature", "download_nature_pdfs.py"),
)
PROVIDER_ATTEMPT_CAPS = {
    # Morning 13 + afternoon 8; each invocation is bounded by split policy.
    "optica": 21,
}
PROVIDER_DAILY_ATTEMPT_CAPS = {
    # Preserve the shared daily cap while testing slower afternoon pacing.
    "optica": 21,
}
OPTICA_SPLIT_MORNING_LIMIT = 13
OPTICA_SPLIT_AFTERNOON_LIMIT = 8
DAILY_LIMIT = 30
MAX_DAILY_LIMIT_OVERRIDE = 40
EXIT_SKIPPED_LOCKED = 75
EXIT_DEFERRED = 76
EXIT_IDLE = 77
PROVIDER_SUMMARY_RE = re.compile(
    r"^\[완료\]\s*성공\s+(\d+)\s*/\s*건너뜀\s+(\d+)\s*/\s*실패\s+(\d+)\s*$"
)

load_dotenv(ROOT / ".env")
DB = dict(
    host=os.getenv("DB_HOST", "127.0.0.1"),
    port=int(os.getenv("DB_PORT", "3306")),
    user=os.getenv("DB_USER", "root"),
    password=os.getenv("DB_PASSWORD", ""),
    database=os.getenv("DB_NAME", "ieee_repo"),
    charset="utf8mb4",
)


def effective_daily_limit(today: str) -> tuple[int, dict[str, object] | None]:
    """Return the default limit or a tightly scoped, date-matched override.

    The override file is intentionally ignored unless its date exactly matches
    the local run date and its limit stays between the normal and hard maximum.
    This makes a one-day approval expire automatically without a cleanup job.
    """
    override_path = STATE_DIR / "daily_limit_override.json"
    try:
        override = json.loads(override_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return DAILY_LIMIT, None

    if not isinstance(override, dict) or override.get("date") != today:
        return DAILY_LIMIT, None
    limit = override.get("limit")
    if isinstance(limit, bool) or not isinstance(limit, int):
        return DAILY_LIMIT, None
    if not DAILY_LIMIT <= limit <= MAX_DAILY_LIMIT_OVERRIDE:
        return DAILY_LIMIT, None
    return limit, override


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def save_state(state: dict) -> None:
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, STATE_PATH)


def record_outcome(state: dict, status: str, **details) -> None:
    """Persist the latest outcome and an append-only scheduler audit event."""
    outcome = {
        "at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        **details,
    }
    state["last_outcome"] = outcome
    save_state(state)
    with HISTORY_PATH.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(outcome, ensure_ascii=False) + "\n")


def pending_counts() -> dict[str, int]:
    connection = pymysql.connect(**DB)
    try:
        with connection.cursor() as cursor:
            counts: dict[str, int] = {}
            for provider, _ in PROVIDERS:
                repair_ids = load_repair_ids(provider)
                params: list[object] = ["optica" if provider == "jlt_ieee" else provider]
                repair_sql = ""
                if repair_ids:
                    placeholders = ",".join(["%s"] * len(repair_ids))
                    repair_sql = f" OR article_number IN ({placeholders})"
                    params.extend(repair_ids)
                route_sql = ""
                ieee_route = "(source_name = 'JLT' AND (COALESCE(url, '') LIKE 'https://ieeexplore.ieee.org/document/%%' OR COALESCE(url, '') LIKE 'https://www.ieeexplore.ieee.org/document/%%'))"
                if provider in ("optica", "jlt_ieee"):
                    route_sql = " AND " + (ieee_route if provider == "jlt_ieee" else "NOT " + ieee_route)
                cursor.execute(
                    "SELECT COUNT(*) FROM papers "
                    "WHERE pdf_available=0 AND source_system=%s "
                    f"AND (is_favorite=1{repair_sql})" + route_sql,
                    params,
                )
                counts[provider] = int(cursor.fetchone()[0])
            return counts
    finally:
        connection.close()


def provider_allowance(
    provider: str,
    remaining: int,
    pending: int,
    *,
    provider_used: int = 0,
    invocation_cap: int | None = None,
) -> int:
    """Return allowance after per-run and cumulative provider safety caps."""
    allowance = min(max(0, remaining), max(0, pending))
    cap = PROVIDER_ATTEMPT_CAPS.get(provider)
    if cap is not None:
        allowance = min(allowance, cap)
    daily_cap = PROVIDER_DAILY_ATTEMPT_CAPS.get(provider)
    if daily_cap is not None:
        allowance = min(allowance, max(0, daily_cap - max(0, provider_used)))
    if invocation_cap is not None:
        allowance = min(allowance, max(0, invocation_cap))
    return allowance


def split_policy_invocation_cap(
    provider: str,
    today: str,
    state: dict,
    *,
    split_morning_invocation: bool,
) -> int | None:
    """Bound morning and afternoon Optica invocations separately."""
    if provider == "optica" and not split_morning_invocation:
        return OPTICA_SPLIT_AFTERNOON_LIMIT
    if provider == "optica":
        return OPTICA_SPLIT_MORNING_LIMIT
    return None


def provider_ready(provider: str) -> tuple[bool, str | None]:
    provider_lock = STATE_DIR / f"{provider}.lock"
    if provider_lock.exists():
        try:
            lock_age = time.time() - provider_lock.stat().st_mtime
        except OSError:
            lock_age = 0
        if lock_age > 6 * 3600:
            try:
                provider_lock.unlink()
            except OSError:
                pass
        else:
            return False, "같은 출판사 작업이 이미 실행 중"
    try:
        state = json.loads((STATE_DIR / f"{provider}.json").read_text(encoding="utf-8"))
        until = datetime.fromisoformat(state.get("cooldown_until", ""))
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        if until > datetime.now(timezone.utc):
            return False, state.get("reason") or "쿨다운 중"
    except (OSError, ValueError, TypeError):
        pass
    return True, None


def parse_provider_summary(line: str) -> dict[str, int] | None:
    match = PROVIDER_SUMMARY_RE.match(line.strip())
    if not match:
        return None
    successes, skipped, failures = (int(value) for value in match.groups())
    return {
        "successes": successes,
        "skipped": skipped,
        "failures": failures,
    }


def console_safe_text(value: str, encoding: str | None = None) -> str:
    """Make streamed child output printable on legacy Windows consoles."""
    target_encoding = encoding or getattr(sys.stdout, "encoding", None) or "utf-8"
    return value.encode(target_encoding, errors="replace").decode(target_encoding)


def actual_attempt_count(summary: dict[str, int] | None, allowance: int) -> int:
    """Return publisher requests made, retaining the reservation if unknown."""
    if summary is None:
        return allowance
    attempted = int(summary.get("successes", 0)) + int(summary.get("failures", 0))
    return max(0, min(attempted, allowance))


def classify_outcome(
    provider_return_code: int,
    attempted_providers: list[str],
    pending_total: int,
    successful_downloads: int,
) -> str:
    """Allow verified downloads to reach Zotero even when a later item fails."""
    if provider_return_code and successful_downloads <= 0:
        return "failed"
    if successful_downloads > 0:
        return "completed"
    if pending_total:
        return "deferred"
    return "idle"


def run_provider(command: list[str], child_env: dict[str, str]) -> tuple[int, dict[str, int] | None]:
    """Stream child output to the scheduler log and capture its final counters."""
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    summary = None
    assert process.stdout is not None
    for line in process.stdout:
        parsed = parse_provider_summary(line)
        if parsed is not None:
            summary = parsed
        print(console_safe_text(line), end="", flush=True)
    return int(process.wait()), summary


def main() -> int:
    parser = argparse.ArgumentParser(description="IEEE → Optica → Nature 우선순위 PDF 확보 routine")
    parser.add_argument("--provider", choices=("auto", "ieee", "jlt_ieee", "optica", "nature"),
                        default="auto", help="기본 auto: IEEE → Optica → Nature 순서")
    parser.add_argument("--limit", type=bounded_download_limit, default=30,
                        help="이번 실행에서 사용할 일일 시도 예산 (기본 30, 일일 누적 최대 30)")
    args = parser.parse_args()
    requested_limit = args.limit
    pipeline_run_id: str | None = None

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        try:
            lock_age = time.time() - LOCK_PATH.stat().st_mtime
        except OSError:
            lock_age = 0
        if lock_age > 6 * 3600:
            try:
                LOCK_PATH.unlink()
            except OSError:
                pass
            return main()
        print("[*] PDF routine이 이미 실행 중이므로 이번 실행을 생략합니다.")
        print("[OUTCOME] SKIPPED_LOCKED")
        return EXIT_SKIPPED_LOCKED

    try:
        state = load_state()
        local_now = datetime.now().astimezone()
        today = local_now.date().isoformat()
        daily_limit, daily_limit_override = effective_daily_limit(today)
        if state.get("daily_budget_date") != today:
            state["daily_budget_date"] = today
            state["daily_attempts_reserved"] = 0
            state["daily_provider_attempts_reserved"] = {}
        try:
            used_today = max(0, min(int(state.get("daily_attempts_reserved", 0)), daily_limit))
        except (TypeError, ValueError):
            used_today = 0
        state["daily_attempts_reserved"] = used_today
        raw_provider_usage = state.get("daily_provider_attempts_reserved", {})
        if not isinstance(raw_provider_usage, dict):
            raw_provider_usage = {}
        provider_usage: dict[str, int] = {}
        for provider, _ in PROVIDERS:
            try:
                value = max(0, int(raw_provider_usage.get(provider, 0)))
            except (TypeError, ValueError):
                value = 0
            daily_provider_cap = PROVIDER_DAILY_ATTEMPT_CAPS.get(provider)
            if daily_provider_cap is not None:
                value = min(value, daily_provider_cap)
            provider_usage[provider] = value
        state["daily_provider_attempts_reserved"] = provider_usage
        split_morning_invocation = (
            args.provider == "optica"
            and requested_limit == OPTICA_SPLIT_MORNING_LIMIT
        )
        if split_morning_invocation:
            state["optica_split_morning_date"] = today
        state["daily_limit"] = daily_limit
        state["daily_limit_source"] = (
            "date_scoped_override" if daily_limit_override else "default"
        )

        run_budget = min(requested_limit, daily_limit - used_today)
        if run_budget <= 0:
            state["last_daily_run"] = datetime.now(timezone.utc).isoformat()
            record_outcome(
                state,
                "deferred",
                reason="daily_budget_exhausted",
                daily_budget_used=used_today,
                daily_limit=daily_limit,
            )
            print(f"[*] {today} 일일 시도 예산 {daily_limit}건을 이미 사용하여 생략합니다.")
            print("[OUTCOME] DEFERRED: daily_budget_exhausted")
            return EXIT_DEFERRED

        pipeline_run_id = (
            f"{today}-{datetime.now().astimezone().strftime('%H%M%S')}-{os.getpid()}"
        )
        begin_pipeline(pipeline_run_id, today)
        counts = pending_counts()
        queue = PROVIDERS if args.provider == "auto" else tuple(
            item for item in PROVIDERS if item[0] == args.provider
        )
        remaining = run_budget
        final_return_code = 0
        successful_downloads = 0
        attempted_providers: list[str] = []
        deferrals: list[dict[str, object]] = []
        provider_results: dict[str, dict[str, int]] = {}

        if daily_limit_override:
            print(f"[*] 승인된 일일 한도: {today}에 한해 {daily_limit}건")
        print(
            f"[*] 일일 시도 예산: 최대 {daily_limit}건 / "
            f"오늘 사용 {used_today}건 / 이번 실행 가능 {run_budget}건"
        )
        print("[*] 우선순위: IEEE → Optica → Nature")
        if split_morning_invocation:
            print(
                f"[*] Optica 안전 정책: 오전 최대 {OPTICA_SPLIT_MORNING_LIMIT}건, "
                "오후 최대 8건 (1500~2400초 랜덤 간격)"
            )
        print("[*] 미확보 즐겨찾기: " + ", ".join(
            f"{name} {counts.get(name, 0)}건" for name, _ in PROVIDERS
        ))

        for provider, script in queue:
            if remaining <= 0:
                break
            pending = counts.get(provider, 0)
            if pending <= 0:
                continue
            ready, reason = provider_ready(provider)
            if not ready:
                deferral = {
                    "provider": provider,
                    "pending": pending,
                    "reason": reason or "provider_not_ready",
                }
                deferrals.append(deferral)
                print(f"[보류] {provider}: {deferral['reason']} (대기 {pending}건)")
                continue

            provider_used = provider_usage.get(provider, 0)
            invocation_cap = split_policy_invocation_cap(
                provider,
                today,
                state,
                split_morning_invocation=split_morning_invocation,
            )
            # Preserve the afternoon experiment budget when earlier queues are busy.
            available = remaining
            if args.provider == "auto" and provider in ("ieee", "jlt_ieee"):
                optica_ready, _ = provider_ready("optica")
                if optica_ready:
                    reserve = provider_allowance("optica", remaining, counts.get("optica", 0),
                                                 provider_used=provider_usage.get("optica", 0),
                                                 invocation_cap=OPTICA_SPLIT_AFTERNOON_LIMIT)
                    available = max(0, remaining - reserve)
            allowance = provider_allowance(
                provider,
                available,
                pending,
                provider_used=provider_used,
                invocation_cap=invocation_cap,
            )
            if allowance <= 0:
                daily_provider_cap = PROVIDER_DAILY_ATTEMPT_CAPS.get(provider)
                if provider == "optica" and invocation_cap == 0:
                    deferral = {
                        "provider": provider,
                        "pending": pending,
                        "reason": "optica_morning_only_policy",
                    }
                    deferrals.append(deferral)
                    print("[보류] optica: 오전 전용 정책으로 저녁 실행을 생략합니다.")
                    continue
                deferral = {
                    "provider": provider,
                    "pending": pending,
                    "reason": "provider_daily_budget_exhausted",
                }
                deferrals.append(deferral)
                print(
                    f"[보류] {provider}: 하루 누적 한도 "
                    f"{provider_used}/{daily_provider_cap}건 사용"
                )
                continue
            # 하위 프로세스 실행 전에 예산을 예약한다. 작업이 강제 종료되어도
            # 같은 날 재실행으로 일일 한도를 초과하지 않는다.
            used_today += allowance
            provider_usage[provider] = provider_used + allowance
            state["daily_attempts_reserved"] = used_today
            state["daily_provider_attempts_reserved"] = provider_usage
            state["last_daily_run"] = datetime.now(timezone.utc).isoformat()
            save_state(state)

            command = [sys.executable, str(SCRIPT_DIR / script), "--limit", str(allowance)]
            if provider == "jlt_ieee":
                command += ["--route", "jlt-ieee"]
            elif provider == "optica" and not split_morning_invocation:
                command += ["--min-sleep", "1500", "--max-sleep", "2400", "--batch-size", "0"]
            provider_budget_text = ""
            if provider in PROVIDER_DAILY_ATTEMPT_CAPS:
                provider_budget_text = (
                    f" (오늘 누적 예약 {provider_usage[provider]}/"
                    f"{PROVIDER_DAILY_ATTEMPT_CAPS[provider]}건)"
                )
            print(f"[*] {provider}: 최대 {allowance}건 시도{provider_budget_text}")
            child_env = os.environ.copy()
            child_env["PDF_ROUTINE_CHILD"] = "1"
            return_code, summary = run_provider(command, child_env)
            attempted_providers.append(provider)
            final_return_code = final_return_code or return_code
            if summary is not None:
                provider_results[provider] = summary
                successful_downloads += int(summary.get("successes", 0))

            actual_attempts = actual_attempt_count(summary, allowance)
            unused_reservation = allowance - actual_attempts
            if unused_reservation:
                used_today -= unused_reservation
                provider_usage[provider] = max(
                    0, provider_usage.get(provider, 0) - unused_reservation
                )
                state["daily_attempts_reserved"] = used_today
                state["daily_provider_attempts_reserved"] = provider_usage
                save_state(state)
                print(
                    f"[*] {provider}: 미사용 예약 {unused_reservation}건 반환 "
                    f"(실제 요청 {actual_attempts}건)"
                )
            remaining -= actual_attempts

            state.setdefault("last_runs", {})[provider] = {
                "at": datetime.now(timezone.utc).isoformat(),
                "return_code": return_code,
                "attempt_budget": allowance,
                "actual_attempts": actual_attempts,
                **(summary or {}),
            }

        state["last_daily_run"] = datetime.now(timezone.utc).isoformat()
        state["daily_limit"] = daily_limit
        pending_total = sum(counts.get(provider, 0) for provider, _ in queue)
        outcome_status = classify_outcome(
            final_return_code,
            attempted_providers,
            pending_total,
            successful_downloads,
        )
        effective_return_code = final_return_code if outcome_status == "failed" else 0
        record_outcome(
            state,
            outcome_status,
            return_code=effective_return_code,
            provider_return_code=final_return_code,
            partial_failure=bool(final_return_code and successful_downloads),
            successful_downloads=successful_downloads,
            provider_results=provider_results,
            attempted_providers=attempted_providers,
            deferrals=deferrals,
            pending_counts=counts,
            reserved_this_run=run_budget - remaining,
            daily_budget_used=used_today,
            daily_limit=daily_limit,
            provider_daily_budget_used=provider_usage,
            provider_daily_limits=PROVIDER_DAILY_ATTEMPT_CAPS,
        )
        if outcome_status == "deferred":
            print(f"[보류] 다운로드 대기 {pending_total}건이 있으나 실행 가능한 출판사가 없습니다.")
        elif final_return_code and successful_downloads:
            print(
                f"[OUTCOME] PARTIAL: 성공 {successful_downloads}건을 후처리하고 "
                "실패 내역은 상태에 보존합니다."
            )
        print(
            f"[*] routine 종료: 이번 실행 배정 {run_budget - remaining}건 / "
            f"오늘 누적 예약 {used_today}/{daily_limit}건"
        )
        checkpoint_details = {
            "outcome_status": outcome_status,
            "attempted_providers": attempted_providers,
            "reserved_this_run": run_budget - remaining,
            "partial_failure": bool(final_return_code and successful_downloads),
            "successful_downloads": successful_downloads,
            "provider_results": provider_results,
        }
        if outcome_status == "failed":
            update_stage(
                "download",
                "failed",
                run_id=pipeline_run_id,
                return_code=final_return_code,
                details=checkpoint_details,
            )
        elif outcome_status == "deferred":
            update_stage(
                "download",
                "deferred",
                run_id=pipeline_run_id,
                return_code=0,
                details=checkpoint_details,
            )
            print("[OUTCOME] DEFERRED: no provider was ready")
            return EXIT_DEFERRED
        else:
            update_stage(
                "download",
                "completed",
                run_id=pipeline_run_id,
                return_code=0,
                details=checkpoint_details,
            )
        if successful_downloads == 0:
            if outcome_status == "idle":
                for stage in POST_STAGES:
                    update_stage(stage, "skipped", run_id=pipeline_run_id,
                                 details={"reason": "no_verified_downloads"})
            return effective_return_code or EXIT_IDLE
        return effective_return_code
    except BaseException as exc:
        if pipeline_run_id:
            try:
                update_stage(
                    "download",
                    "failed",
                    run_id=pipeline_run_id,
                    return_code=getattr(exc, "errno", 1) or 1,
                    details={"error": f"{type(exc).__name__}: {exc}"},
                )
            except Exception:
                pass
        raise
    finally:
        try:
            LOCK_PATH.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
