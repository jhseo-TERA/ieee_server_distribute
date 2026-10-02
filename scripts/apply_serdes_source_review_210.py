"""Apply only the approved v2 SerDes metadata corrections.

Default: SELECT-only read-only dry run. --apply is required for writes.
Never imports application startup/migration code. Credentials are not logged.
The source-review plan and original DB snapshot are immutable inputs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
VERSION = "serdes-source-review-210-v2"
DEFAULT_PLAN = ROOT / "outputs/serdes_source_review_210_20260929_v2/metadata_corrections_v2.json"
# Exact user-approved population: no optional enrichments, related points or holds.
APPROVED = {
    (1568, "component_scope"): ("7295549", "tx_rx", "rx"),
    (2311, "component_scope"): ("10171209", "rx", "tx_rx"),
    (1582, "component_scope"): ("6782470", "tx_rx", "tx"),
    (1921, "component_scope"): ("8952650", "tx_rx", "rx"),
    (1714, "component_scope"): ("6169954", "tx_rx", "rx"),
    (1857, "component_scope"): ("9302604", "tx_rx", "tx"),
    (1855, "component_scope"): ("9279272", "tx_rx", "tx"),
    (1584, "component_scope"): ("10310954", "tx", "unknown"),
    (1374, "component_scope"): ("11157760", "tx", "tx_rx"),
    (2143, "process_nm"): ("8310290", 28, 16),
    (2143, "process_text"): ("8310290", "28.0", "16nm FinFET"),
}


class ReviewGuardError(RuntimeError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def scalar(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def canonical(value):
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in value.items()}
    if isinstance(value, list):
        return [canonical(v) for v in value]
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return str(Decimal(str(value)).normalize())
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def equal(left, right) -> bool:
    return canonical(left) == canonical(right)


def require(condition, message):
    if not condition:
        raise ReviewGuardError(message)


def save(path: Path, value):
    # Atomic local report replacement; no remote or credential data is included.
    temporary = path.with_suffix(path.suffix + ".pending")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=scalar) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_plan(path: Path):
    data = path.read_bytes()
    plan = json.loads(data)
    require(plan.get("schema_version") == "serdes-metadata-correction-plan-v2", "Unexpected plan schema")
    original_path = Path(plan["db_snapshot_path"])
    original_data = original_path.read_bytes()
    require(digest(original_data) == plan["db_snapshot_sha256"], "Original DB snapshot hash mismatch")
    original = json.loads(original_data)
    seen = set()
    for change in plan["change_rows"]:
        key = (int(change["measurement_id"]), change["field"])
        require(key in APPROVED and key not in seen, "Unapproved or duplicate field in plan")
        article, before, after = APPROVED[key]
        require(change["table"] == "serdes_measurements", "Unapproved table")
        require(change["article"] == article and equal(change["current"], before)
                and equal(change["proposed"], after), "Plan differs from approved field values")
        require(change["status"] == "supported_draft", "Non-supported change included")
        seen.add(key)
    require(seen == set(APPROVED), "Plan does not contain exactly the approved 11 fields")
    entries = {int(e["measurement_id"]): e for e in plan["entries"]}
    for mid, _ in APPROVED:
        entry = entries[mid]
        require(entry["status"] in {"metadata_before_figure", "metadata_correction"}, "Hold entry cannot be applied")
        for source in entry["pdf_evidence"]:
            require(digest(Path(source["path"]).read_bytes()) == source["sha256"], "Source PDF hash mismatch")
    return plan, original, entries, digest(data)


def expected_evidence(mid, field, entry, paper):
    old, new = APPROVED[(mid, field)][1:]
    pages = sorted({p["page"] for p in entry["pdf_evidence"]})
    excerpts = list(dict.fromkeys(p["excerpt"] for p in entry["pdf_evidence"]))
    text = (f"{VERSION}; article={entry['article']}; measurement_id={mid}; "
            f"{field}: {old} -> {new}. {entry['reason']} " + " ".join(excerpts))
    text_hash = digest(text.encode("utf-8"))
    key = digest(f"{mid}|{field}|pdf|{text_hash}".encode("utf-8"))
    return {
        "evidence_key": key, "measurement_id": mid, "field_name": field,
        "paper_id": paper["paper_id"], "abstract_id": None, "source_kind": "pdf",
        "source_url": f"https://ieeexplore.ieee.org/document/{entry['article']}",
        "source_locator": "PDF p." + ",".join(map(str, pages)) + "; " + VERSION,
        "evidence_text": text, "source_sha256": entry["pdf_evidence"][0]["sha256"],
        "evidence_sha256": text_hash, "extraction_method": "source_pdf_manual_review",
        "extractor_version": VERSION, "confidence": Decimal("0.990"), "review_status": "verified",
    }


def inspect_state(cursor, guard_ids):
    marks = ",".join(["%s"] * len(guard_ids))
    def read(sql, args=None):
        cursor.execute(sql, args)
        return cursor.fetchall()
    return {
        "measurements": read(f"SELECT * FROM serdes_measurements WHERE id IN ({marks}) ORDER BY id", guard_ids),
        "evidence": read(f"SELECT * FROM serdes_measurement_evidence WHERE measurement_id IN ({marks}) ORDER BY id", guard_ids),
        # All rows, not only the 10 targets: no scope write is allowed.
        "metric_scopes_all": read("SELECT * FROM serdes_measurement_metric_scopes ORDER BY measurement_id"),
        "scope_overrides_all": read("SELECT * FROM serdes_measurement_scope_overrides ORDER BY measurement_id"),
    }


def validate_targets(before, original, entries, paper_links):
    current = {r["id"]: r for r in before["measurements"]}
    baseline = {r["id"]: r for r in original["measurements"]}
    evidence_by_key = {r["evidence_key"]: r for r in before["evidence"]}
    pending, completed, new_evidence = [], [], []
    for mid in sorted({key[0] for key in APPROVED}):
        row, old = current[mid], baseline[mid]
        fields = {f for m, f in APPROVED if m == mid}
        entry = entries[mid]
        matches = [p for p in paper_links if p["measurement_id"] == mid and str(p["article_number"]) == entry["article"]]
        require(len(matches) == 1, f"Measurement {mid} no longer has its expected paper association")
        for field in row:
            if field not in fields | {"updated_at"}:
                require(equal(row[field], old[field]), f"Measurement {mid}: unrelated field changed: {field}")
        all_old = all(equal(row[f], APPROVED[(mid, f)][1]) for f in fields)
        all_new = all(equal(row[f], APPROVED[(mid, f)][2]) for f in fields)
        require(all_old or all_new, f"Measurement {mid}: neither approved before-state nor complete after-state")
        records = [expected_evidence(mid, f, entry, matches[0]) for f in sorted(fields)]
        if all_old:
            require(equal(row["updated_at"], old["updated_at"]), f"Measurement {mid}: updated_at precondition failed")
            require(all(e["evidence_key"] not in evidence_by_key for e in records), f"Measurement {mid}: evidence exists but fields reverted; inspect prior rollback")
            pending.append(mid)
            new_evidence.extend(records)
        else:
            for expected in records:
                actual = evidence_by_key.get(expected["evidence_key"])
                require(actual is not None, f"Measurement {mid}: proposed values exist without this review's evidence")
                require(all(equal(actual[k], v) for k, v in expected.items()), f"Measurement {mid}: existing review evidence differs")
            completed.append(mid)
    return pending, completed, new_evidence


def verify_after(before, after, pending, additions):
    b_rows = {r["id"]: r for r in before["measurements"]}
    a_rows = {r["id"]: r for r in after["measurements"]}
    require(set(b_rows) == set(a_rows), "Guard population changed")
    for mid, b in b_rows.items():
        for field, value in b.items():
            if mid in pending and (mid, field) in APPROVED:
                require(equal(a_rows[mid][field], APPROVED[(mid, field)][2]), "Approved value was not applied")
            elif mid in pending and field == "updated_at":
                continue
            else:
                require(equal(a_rows[mid][field], value), f"Protected measurement {mid} field {field} changed")
    for table in ["metric_scopes_all", "scope_overrides_all"]:
        require(equal(before[table], after[table]), f"Protected {table} changed")
    b_evidence = {r["id"]: r for r in before["evidence"]}
    a_evidence = {r["id"]: r for r in after["evidence"]}
    require(all(equal(a_evidence.get(i), r) for i, r in b_evidence.items()), "Existing evidence was modified")
    inserted = [r for i, r in a_evidence.items() if i not in b_evidence]
    require({r["evidence_key"] for r in inserted} == {r["evidence_key"] for r in additions}, "Unexpected evidence insertion")
    for expected in additions:
        actual = next(r for r in inserted if r["evidence_key"] == expected["evidence_key"])
        require(all(equal(actual[k], v) for k, v in expected.items()), "Inserted PDF evidence mismatch")
    return inserted


def run(args):
    plan, original, entries, plan_hash = load_plan(args.plan)
    run_dir = args.output_dir / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ("-apply" if args.apply else "-dry-run"))
    run_dir.mkdir(parents=True, exist_ok=False)
    sys.path.append(str(ROOT / ".venv/Lib/site-packages"))
    import pymysql
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    conn = pymysql.connect(host=os.getenv("DB_HOST", "127.0.0.1"), port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER", "root"), password=os.getenv("DB_PASSWORD", ""), database=os.getenv("DB_NAME", "ieee_repo"),
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor, autocommit=False, connect_timeout=10)
    committed = False
    result = {"run_dir": str(run_dir), "mode": "apply" if args.apply else "read_only_dry_run", "plan_sha256": plan_hash,
              "started_at_utc": datetime.now(timezone.utc).isoformat(), "committed": False}
    try:
        with conn.cursor() as cur:
            cur.execute("START TRANSACTION" if args.apply else "START TRANSACTION READ ONLY")
            target_ids = sorted({mid for mid, _ in APPROVED})
            marks = ",".join(["%s"] * len(target_ids))
            if args.apply:
                cur.execute(f"SELECT id FROM serdes_measurements WHERE id IN ({marks}) ORDER BY id FOR UPDATE", target_ids)
                require(len(cur.fetchall()) == len(target_ids), "Target row missing")
            guard_ids = sorted({r["id"] for r in original["measurements"] + original["related_measurements"]})
            before = inspect_state(cur, guard_ids)
            save(run_dir / "before.json", before)
            cur.execute(f"SELECT m.id measurement_id,ip.paper_id,p.article_number FROM serdes_measurements m JOIN serdes_implementation_papers ip ON ip.implementation_id=m.implementation_id JOIN papers p ON p.id=ip.paper_id WHERE m.id IN ({marks})", target_ids)
            links = cur.fetchall()
            pending, completed, additions = validate_targets(before, original, entries, links)
            result.update(pending_measurement_ids=pending, already_applied_measurement_ids=completed,
                          pending_field_count=sum(1 for m, _ in APPROVED if m in pending), expected_new_evidence_count=len(additions))
            rollback = {"status": "plan_only_not_executed", "plan_sha256": plan_hash,
                        "instructions": ["Use a new explicit transaction after checking post-apply values and updated_at from after.json.",
                                         "Restore only these fields with compare-before-write guards; never restore the whole row.",
                                         "Keep new PDF evidence as historical source review; never delete old or new scholarly evidence.",
                                         "Do not touch hold points, related points, NULL energy, metric scopes or scope overrides."],
                        "restore_fields": [{"measurement_id": m, "field": f, "expected_applied": values[2], "restore_value": values[1]} for (m, f), values in APPROVED.items() if m in pending],
                        "new_evidence_keys_to_retain": [e["evidence_key"] for e in additions]}
            save(run_dir / "rollback_plan.json", rollback)
            save(run_dir / "planned_evidence.json", additions)
            if args.apply:
                before_by_id = {r["id"]: r for r in before["measurements"]}
                for mid in pending:
                    fields = sorted(f for m, f in APPROVED if m == mid)
                    # SQL identifiers come only from the hard-coded allowlist above.
                    assignments = ",".join(f"`{f}`=%s" for f in fields)
                    guards = " AND ".join(f"`{f}` <=> %s" for f in fields)
                    params = [APPROVED[(mid, f)][2] for f in fields] + [mid] + [APPROVED[(mid, f)][1] for f in fields] + [before_by_id[mid]["updated_at"]]
                    cur.execute(f"UPDATE serdes_measurements SET {assignments} WHERE id=%s AND {guards} AND updated_at <=> %s", params)
                    require(cur.rowcount == 1, f"Compare-before-write failed for measurement {mid}")
                for record in additions:
                    columns = list(record)
                    cur.execute("INSERT INTO serdes_measurement_evidence (" + ",".join(columns) + ") VALUES (" + ",".join(["%s"] * len(columns)) + ")", [record[k] for k in columns])
                after = inspect_state(cur, guard_ids)
                inserted = verify_after(before, after, pending, additions)
                result["inserted_evidence_ids"] = [r["id"] for r in inserted]
                save(run_dir / "after.json", after)
                rollback["post_apply_updated_at"] = {str(r["id"]): r["updated_at"] for r in after["measurements"] if r["id"] in pending}
                rollback["inserted_evidence_ids_to_retain"] = result["inserted_evidence_ids"]
                save(run_dir / "rollback_plan.json", rollback)
                save(run_dir / "result.json", {**result, "status": "validated_before_commit"})
                conn.commit()
                committed = True
                result.update(committed=True, status="applied" if pending else "already_applied_no_changes")
            else:
                after = inspect_state(cur, guard_ids)
                require(equal(before, after), "Read-only dry run snapshot unexpectedly changed")
                save(run_dir / "after.json", after)
                conn.rollback()
                result["status"] = "dry_run_passed"
            result["verification"] = {"approved_fields_only": True, "old_evidence_preserved": True,
                "all_energy_metric_scope_rows_preserved": True, "all_scope_overrides_preserved": True,
                "NULL_energy_and_all_other_measurement_fields_preserved": True, "hold_and_related_points_preserved": True}
            save(run_dir / "result.json", result)
            return result
    except Exception as exc:
        if not committed:
            conn.rollback()
        failure = {**result, "committed": committed, "status": "post_commit_report_failure" if committed else "rolled_back_or_read_only_failure",
                   "error_type": type(exc).__name__}
        if isinstance(exc, ReviewGuardError):
            failure["guard_message"] = str(exc)
        save(run_dir / "result.json", failure)
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "tmp/codex_apply_review_210/db")
    parser.add_argument("--apply", action="store_true", help="Write the approved 11 fields and new PDF evidence in one transaction")
    args = parser.parse_args()
    try:
        result = run(args)
        print(json.dumps(result, ensure_ascii=False, default=scalar))
        return 0
    except Exception as exc:
        # DB exceptions can contain connection details. Never echo their text.
        message = str(exc) if isinstance(exc, ReviewGuardError) else "Details suppressed; inspect safe run result.json."
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "message": message}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
