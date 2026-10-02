# -*- coding: utf-8 -*-
"""Shared, conservative safety controls for publisher PDF downloads.

This module does not attempt to bypass publisher controls.  It prevents a
scheduled job from repeatedly contacting a provider after throttling or access
denial, and stops a run when repeated failures suggest that access is unhealthy.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError


ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "logs" / "pdf_download_state"
REPAIR_QUEUE_PATH = STATE_DIR / "repair_queue.json"
MAX_DAILY_DOWNLOADS = 30
ITEM_FAILURE_BACKOFF_DAYS = (1, 3, 7, 14, 30)


def bounded_download_limit(value: str) -> int:
    """Argparse type for the only supported per-run range: 1..30."""
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("다운로드 수는 정수여야 합니다.") from exc
    if not 1 <= limit <= MAX_DAILY_DOWNLOADS:
        raise argparse.ArgumentTypeError(
            f"다운로드 수는 1~{MAX_DAILY_DOWNLOADS}건만 허용됩니다."
        )
    return limit


def invoked_by_daily_routine() -> bool:
    """Only the locked daily routine may launch a provider downloader."""
    return (
        os.getenv("PDF_ROUTINE_CHILD") == "1"
        and (STATE_DIR / "routine.lock").exists()
    )


def download_result_code(successes: int, failures: int, *, interrupted: bool = False) -> int:
    """Translate a downloader outcome into a scheduler-visible exit code."""
    if interrupted:
        return 130
    return 1 if failures else 0


def validate_pdf_file(path: str | Path) -> tuple[bool, str | None]:
    """Validate the PDF catalog and all page references before accepting it."""
    candidate = Path(path)
    try:
        io_path = str(candidate.resolve())
        if os.name == "nt" and not io_path.startswith("\\\\?\\"):
            if io_path.startswith("\\\\"):
                io_path = "\\\\?\\UNC\\" + io_path[2:]
            else:
                io_path = "\\\\?\\" + io_path
        with open(io_path, "rb") as handle:
            if handle.read(5) != b"%PDF-":
                return False, "PDF header is missing"
            handle.seek(0)
            reader = PdfReader(handle, strict=False)
            if reader.is_encrypted and reader.decrypt("") == 0:
                return False, "encrypted PDF cannot be opened"
            if len(reader.pages) < 1:
                return False, "PDF has no pages"
            for page in reader.pages:
                _ = page.mediabox
        return True, None
    except (OSError, PdfReadError, ValueError, TypeError, KeyError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _load_repair_queue() -> dict:
    try:
        data = json.loads(REPAIR_QUEUE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"items": []}
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return {"items": []}
    return data


def _save_repair_items(items: list[dict]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": _utcnow().isoformat(),
        "items": items,
    }
    temporary = REPAIR_QUEUE_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, REPAIR_QUEUE_PATH)


def load_repair_ids(provider: str) -> tuple[str, ...]:
    """Return queued article IDs for a provider in detection order."""
    result: list[str] = []
    seen: set[str] = set()
    for item in _load_repair_queue()["items"]:
        if not isinstance(item, dict) or item.get("provider") != provider:
            continue
        article_number = str(item.get("article_number") or "").strip()
        if article_number and article_number not in seen:
            seen.add(article_number)
            result.append(article_number)
    return tuple(result)


def queue_repairs(items: list[dict]) -> None:
    """Merge integrity findings into the persistent high-priority repair queue."""
    current = [
        item for item in _load_repair_queue()["items"]
        if isinstance(item, dict)
    ]
    positions = {
        (str(item.get("provider")), str(item.get("article_number"))): index
        for index, item in enumerate(current)
    }
    for item in items:
        key = (str(item.get("provider")), str(item.get("article_number")))
        if not key[0] or not key[1]:
            continue
        if key in positions:
            current[positions[key]] = item
        else:
            positions[key] = len(current)
            current.append(item)
    _save_repair_items(current)


def mark_repair_complete(provider: str, article_number: str) -> None:
    """Remove a repaired article from the queue after a successful download."""
    article_number = str(article_number)
    data = _load_repair_queue()
    remaining = [
        item for item in data["items"]
        if not (
            isinstance(item, dict)
            and item.get("provider") == provider
            and str(item.get("article_number")) == article_number
        )
    ]
    if len(remaining) != len(data["items"]):
        _save_repair_items(remaining)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _item_failure_path(provider: str) -> Path:
    normalized = str(provider or "").strip().lower()
    if not normalized or not normalized.replace("_", "").isalnum():
        raise ValueError("provider contains unsupported characters")
    return STATE_DIR / f"{normalized}_item_failures.json"


def _load_item_failures(provider: str) -> dict:
    path = _item_failure_path(provider)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"provider": provider, "items": {}}
    if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
        return {"provider": provider, "items": {}}
    return data


def _save_item_failures(provider: str, items: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _item_failure_path(provider)
    payload = {
        "provider": provider,
        "updated_at": _utcnow().isoformat(),
        "items": items,
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def deferred_item_ids(provider: str, *, now: datetime | None = None) -> tuple[str, ...]:
    """Return item IDs whose ordinary per-item failures are still cooling down."""
    current = now or _utcnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    deferred: list[str] = []
    for article_number, item in _load_item_failures(provider)["items"].items():
        if not isinstance(item, dict):
            continue
        retry_after = _parse_timestamp(item.get("retry_after"))
        if retry_after and retry_after > current:
            deferred.append(str(article_number))
    return tuple(deferred)


def record_item_failure(
    provider: str,
    article_number: str,
    reason: str,
    *,
    now: datetime | None = None,
) -> datetime:
    """Persist bounded per-item backoff so one bad record cannot head-block a queue."""
    current = now or _utcnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    article_number = str(article_number or "").strip()
    if not article_number:
        raise ValueError("article_number is required")
    data = _load_item_failures(provider)
    items = data["items"]
    previous = items.get(article_number)
    previous_count = previous.get("failures", 0) if isinstance(previous, dict) else 0
    try:
        failure_count = max(0, int(previous_count)) + 1
    except (TypeError, ValueError):
        failure_count = 1
    days = ITEM_FAILURE_BACKOFF_DAYS[
        min(failure_count - 1, len(ITEM_FAILURE_BACKOFF_DAYS) - 1)
    ]
    retry_after = current + timedelta(days=days)
    items[article_number] = {
        "failures": failure_count,
        "reason": str(reason or "unspecified")[:120],
        "last_failed_at": current.isoformat(),
        "retry_after": retry_after.isoformat(),
    }
    _save_item_failures(provider, items)
    return retry_after


def clear_item_failure(provider: str, article_number: str) -> None:
    """Forget item backoff after a verified download or local-file reconciliation."""
    data = _load_item_failures(provider)
    items = data["items"]
    if items.pop(str(article_number), None) is not None:
        _save_item_failures(provider, items)


def retry_after_seconds(response, default: float) -> float:
    """Return a bounded Retry-After delay without retrying the response."""
    raw = (getattr(response, "headers", {}) or {}).get("Retry-After")
    if raw:
        try:
            return max(default, float(raw))
        except (TypeError, ValueError):
            try:
                when = parsedate_to_datetime(raw)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                return max(default, (when - _utcnow()).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return default


class DownloadGuard:
    """Persistent cooldown, single-run lock, pacing and circuit breaker."""

    def __init__(
        self,
        provider: str,
        *,
        min_delay: float,
        max_delay: float,
        cooldown_hours: float = 12.0,
        max_consecutive_failures: int = 3,
    ) -> None:
        self.provider = provider.lower()
        self.min_delay = max(0.0, min_delay)
        self.max_delay = max(self.min_delay, max_delay)
        self.cooldown_hours = max(1.0, cooldown_hours)
        self.max_consecutive_failures = max(1, max_consecutive_failures)
        self.consecutive_failures = 0
        self._lock_owned = False
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.state_path = STATE_DIR / f"{self.provider}.json"
        self.lock_path = STATE_DIR / f"{self.provider}.lock"

    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}

    def _save(self, state: dict) -> None:
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, self.state_path)

    def acquire(self) -> tuple[bool, str | None]:
        state = self._load()
        until = _parse_timestamp(state.get("cooldown_until"))
        if until and until > _utcnow():
            local_until = until.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
            reason = state.get("reason") or "publisher cooldown"
            return False, f"{reason}; 다음 시도 가능: {local_until}"

        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f"pid={os.getpid()}\nstarted={_utcnow().isoformat()}\n")
            self._lock_owned = True
        except FileExistsError:
            try:
                age = time.time() - self.lock_path.stat().st_mtime
            except OSError:
                age = 0
            if age > 6 * 3600:
                try:
                    self.lock_path.unlink()
                except OSError:
                    pass
                return self.acquire()
            return False, "같은 출판사 다운로드 작업이 이미 실행 중입니다."
        return True, None

    def close(self) -> None:
        if self._lock_owned:
            try:
                self.lock_path.unlink()
            except OSError:
                pass
            self._lock_owned = False

    def pace(self) -> None:
        time.sleep(random.uniform(self.min_delay, self.max_delay))

    def success(self) -> None:
        self.consecutive_failures = 0

    def failure(self) -> bool:
        """Return True when the run should stop."""
        self.consecutive_failures += 1
        if self.consecutive_failures < self.max_consecutive_failures:
            return False
        self.cooldown(
            f"연속 실패 {self.consecutive_failures}회",
            hours=min(self.cooldown_hours, 6.0),
        )
        return True

    def cooldown(self, reason: str, *, hours: float | None = None, seconds: float | None = None) -> None:
        delay = seconds if seconds is not None else (hours or self.cooldown_hours) * 3600
        delay = max(3600.0, min(float(delay), 7 * 24 * 3600.0))
        until = _utcnow() + timedelta(seconds=delay)
        self._save(
            {
                "provider": self.provider,
                "reason": reason,
                "cooldown_until": until.isoformat(),
                "updated_at": _utcnow().isoformat(),
            }
        )
        print(f"[안전 중단] {reason}; {delay / 3600:.1f}시간 동안 자동 재시도하지 않습니다.")


def add_guard_arguments(parser, *, min_delay: float, max_delay: float) -> None:
    parser.add_argument("--min-sleep", type=float, default=min_delay,
                        help=f"요청 사이 최소 대기초 (기본 {min_delay:g})")
    parser.add_argument("--max-sleep", type=float, default=max_delay,
                        help=f"요청 사이 최대 대기초 (기본 {max_delay:g})")
    parser.add_argument("--cooldown-hours", type=float, default=12.0,
                        help="차단/속도제한 감지 후 재시도 금지 시간 (기본 12)")
    parser.add_argument("--max-consecutive-failures", type=int, default=3,
                        help="연속 실패 시 실행 중단 기준 (기본 3)")
