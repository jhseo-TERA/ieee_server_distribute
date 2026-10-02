"""Apply a source-grounded, measured-hardware review in one transaction.

The default is a read-only preflight. Inputs include the immutable database
snapshot, qualified-paper list, and field-level PDF evidence. Existing numeric
values, implementation families, and manual holds are preserved. No migrations
or application startup hooks run here.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
VERSION = "serdes-measured-review-1.0"
PROTECTED_ARTICLES = frozenset({"9830507", "9062925"})
NUMERIC_FIELDS = frozenset({
    "process_nm", "reported_rate_gbps", "reported_rate_min_gbps",
    "reported_rate_max_gbps", "lane_rate_gbps", "lane_count",
    "aggregate_rate_gbps", "symbol_rate_gbaud", "power_mw", "energy_pj_bit",
    "energy_loss_normalized_pj_bit_db", "channel_loss_db", "loss_frequency_ghz",
    "ber", "active_area_mm2", "throughput_density_gbps_per_mm",
})
MEASUREMENT_COLUMNS = (
    "implementation_id", "operating_point_key", "reference_title",
    "publication_year", "publication_name", "first_author", "affiliation",
    "link_class", "component_scope", "modulation", "process_text", "process_nm",
    "reported_rate_text", "reported_rate_gbps", "reported_rate_min_gbps",
    "reported_rate_max_gbps", "rate_scope", "lane_rate_gbps", "lane_count",
    "aggregate_rate_gbps", "aggregate_rate_basis", "symbol_rate_gbaud",
    "throughput_density_gbps_per_mm", "power_mw", "power_scope", "energy_pj_bit",
    "energy_scope", "energy_basis", "energy_loss_normalized_pj_bit_db",
    "channel_loss_db", "loss_frequency_ghz", "ber", "ber_scope", "active_area_mm2",
    "source_kind", "overall_confidence", "review_status",
)
EVIDENCE_COLUMNS = (
    "evidence_key", "measurement_id", "field_name", "paper_id", "abstract_id",
    "source_kind", "source_url", "source_locator", "evidence_text",
    "source_sha256", "evidence_sha256", "extraction_method", "extractor_version",
    "confidence", "review_status",
)
SCREEN_COLUMNS = (
    "paper_id", "run_id", "abstract_id", "venue", "scope_version",
    "relevance_class", "relevance_score", "include_in_survey", "reason_codes",
    "rationale", "screening_source", "review_status", "screened_at",
)


def scalar(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    raise TypeError(type(value).__name__)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=scalar,
                      separators=(",", ":"))


def fingerprint(rows):
    return digest("\n".join(sorted(canonical_json(row) for row in rows)).encode())


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".pending")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=scalar)
                       + "\n", encoding="utf-8")
    pending.replace(path)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_numeric(field, value):
    if field not in NUMERIC_FIELDS:
        return
    number = Decimal(str(value))
    require(number.is_finite() and number > 0, f"Invalid {field}")
    if field == "ber":
        require(number <= 1, "Invalid BER probability")
    if field == "lane_count":
        require(number % 1 == 0, "Fractional lane count")


def validate_plan(plan, original, qualified):
    require(plan["version"] == VERSION, "Unexpected plan version")
    approved = {int(row["paper_id"]): row for row in qualified["rows"]}
    require(len(approved) == len(qualified["rows"]), "Duplicate qualified paper")
    require(all(row["verdict"] in {"반영 후보", "기존 보강"}
                and row["source_system"] in {"ieee", "optica"}
                for row in approved.values()), "Unqualified paper in apply scope")
    require(set(approved) == {int(r["paper_id"]) for r in plan["targets"]},
            "Apply scope differs from qualified population")
    require(len(plan["targets"]) == len(approved), "Duplicate apply target")
    papers = {int(r["id"]): r for r in original["papers"]}
    mids = {int(r["id"]): r for r in original["tables"]["serdes_measurements"]}
    links = {(int(r["paper_id"]), int(r["implementation_id"]))
             for r in original["tables"]["serdes_implementation_papers"]}
    sources = {(str(Path(s["path"])), s["sha256"]) for s in plan["sources"]}
    require(all((str(Path(row["pdf_path"])), row["pdf_sha256"]) in sources
                for row in approved.values()), "Qualified PDF missing from source verification")
    new_keys = set()
    for target in plan["targets"]:
        pid = int(target["paper_id"])
        paper, source = papers[pid], approved[pid]
        require(str(paper["article_number"]) == str(source["article"])
                and paper["source_system"] == source["source_system"],
                f"Paper identity mismatch: {pid}")
        require(target["screening"]["paper_id"] == pid
                and target["screening"]["include_in_survey"] == 1
                and target["screening"]["relevance_class"] in {"core", "adjacent"},
                f"Invalid positive screening: {pid}")
        protected = str(paper["article_number"]) in PROTECTED_ARTICLES
        if target.get("implementation_id") is not None:
            require((pid, int(target["implementation_id"])) in links,
                    f"Implementation not linked to target: {pid}")
        else:
            implementation = target.get("implementation")
            require(implementation and implementation["canonical_paper_id"] == pid,
                    f"Invalid new implementation identity: {pid}")
        for update in target["updates"]:
            require(not protected, f"Protected paper update: {pid}")
            mid = int(update["measurement_id"])
            require((pid, int(mids[mid]["implementation_id"])) in links,
                    f"Measurement not linked to target: {mid}")
            for field, value in update["fields"].items():
                require(field in NUMERIC_FIELDS or field in {"process_text", "energy_scope", "energy_basis"},
                        f"Unapproved existing field: {field}")
                coupled_provenance = field in {"energy_scope", "energy_basis"} and "energy_pj_bit" in update["fields"]
                require(mids[mid].get(field) is None or
                        (coupled_provenance and mids[mid].get(field) == "unknown"),
                        f"Existing value overwrite: {mid}/{field}")
                if field in {"energy_scope", "energy_basis"}:
                    require(coupled_provenance, "Provenance change without new energy")
                require(value is not None, "Null enrichment")
                validate_numeric(field, value)
                require(any(e.get("measurement_id") == mid and e["field_name"] == field
                            for e in target["evidence"]),
                        f"Missing enrichment evidence: {mid}/{field}")
        for point in target["new_points"]:
            require(not protected, f"Protected paper new point: {pid}")
            values = point["values"]
            key = (pid, values["operating_point_key"])
            require(key not in new_keys, "Duplicate planned operating point")
            new_keys.add(key)
            require(set(values).issubset(set(MEASUREMENT_COLUMNS)), "Unknown measurement field")
            require(values["source_kind"] == "pdf", "New point must have PDF source")
            require(any(values.get(f) is not None for f in NUMERIC_FIELDS),
                    "Empty numeric point")
            require(values.get("component_scope") in {"tx", "rx", "tx_rx", "full_link", "unknown"},
                    "Invalid component scope")
            for field in NUMERIC_FIELDS:
                if values.get(field) is not None:
                    validate_numeric(field, values[field])
                    require(any(e["field_name"] == field for e in point["evidence"]),
                            f"Missing field-level evidence: {pid}/{field}")
            scope = point.get("energy_component_scope", "unknown")
            require(scope in {"tx", "rx", "trx", "full_link", "driver_only", "unknown"},
                    "Invalid energy component scope")
        for evidence in target["evidence"] + [e for point in target["new_points"] for e in point["evidence"]]:
            require(evidence["paper_id"] == pid and evidence["source_kind"] == "pdf",
                    "Invalid evidence association")
            require(evidence["source_sha256"] == source["pdf_sha256"], "PDF hash mismatch")
            require(0 < len(evidence["field_name"]) <= 48
                    and 0 < len(evidence["source_locator"]) <= 120, "Evidence schema overflow")
            require(evidence["evidence_text"].strip(), "Empty source evidence")
        for evidence in target["evidence"]:
            mid = int(evidence["measurement_id"])
            require(mid in mids and (pid, int(mids[mid]["implementation_id"])) in links,
                    f"Evidence measurement not linked to target: {mid}")
        for table, values in target.get("new_classifications", {}).items():
            require(table in {"serdes_paper_link_media", "serdes_paper_link_subtypes"},
                    "Unapproved classification table")
            require(values["paper_id"] == pid and not any(
                row["paper_id"] == pid for row in original["tables"][table]),
                f"Existing classification overwrite: {pid}")
    for table, rows in original["tables"].items():
        require(plan["before_fingerprints"][table] == fingerprint(rows),
                f"Snapshot fingerprint mismatch: {table}")


def connect():
    import os
    import pymysql
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    return pymysql.connect(host=os.getenv("DB_HOST", "127.0.0.1"),
                           port=int(os.getenv("DB_PORT", "3306")),
                           user=os.getenv("DB_USER", "root"),
                           password=os.getenv("DB_PASSWORD"),
                           database=os.getenv("DB_NAME", "ieee_repo"),
                           charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
                           autocommit=False)


def read_tables(cur, names, lock=False):
    state = {}
    for name in names:
        require(name.startswith("serdes_") and name.replace("_", "").isalnum(),
                "Invalid table name")
        cur.execute(f"SELECT * FROM `{name}` ORDER BY 1" + (" FOR UPDATE" if lock else ""))
        state[name] = cur.fetchall()
    return state


def insert_row(cur, table, values, columns):
    marks = ",".join(["%s"] * len(columns))
    cur.execute(f"INSERT INTO `{table}` ({','.join(columns)}) VALUES ({marks})",
                tuple(values.get(column) for column in columns))
    return cur.lastrowid


def store_evidence(cur, evidence, mid, seen):
    text_hash = digest(evidence["evidence_text"].encode())
    key = digest(f"{mid}|{evidence['field_name']}|pdf|{text_hash}".encode())
    if key in seen:
        return 0
    values = {**evidence, "evidence_key": key, "measurement_id": mid,
              "evidence_sha256": text_hash, "abstract_id": None,
              "extraction_method": "source_pdf_measured_review",
              "extractor_version": VERSION}
    insert_row(cur, "serdes_measurement_evidence", values, EVIDENCE_COLUMNS)
    seen.add(key)
    return 1


def assert_post_state(before, after, plan, receipt):
    """Verify non-target data and held measurements before committing."""
    targets = {t["paper_id"] for t in plan["targets"]}
    carried = set(receipt["carried_paper_ids"])
    allowed_updates = {}
    for t in plan["targets"]:
        for update in t["updates"]:
            allowed_updates.setdefault(update["measurement_id"], {}).update(update["fields"])
    new_mids = set(receipt["new_measurement_ids"])
    old_m = {r["id"]: r for r in before["serdes_measurements"]}
    now_m = {r["id"]: r for r in after["serdes_measurements"]}
    require(set(now_m) - set(old_m) == new_mids, "Unexpected new measurement")
    require(set(old_m).issubset(now_m), "Existing measurement removed")
    for mid, old in old_m.items():
        fields = allowed_updates.get(mid, {})
        for key, value in old.items():
            if key not in fields and not (fields and key == "updated_at"):
                require(canonical_json(now_m[mid][key]) == canonical_json(value),
                        f"Unrelated measurement changed: {mid}/{key}")
        for field, value in fields.items():
            require(Decimal(str(now_m[mid][field])) == Decimal(str(value))
                    if field in NUMERIC_FIELDS else now_m[mid][field] == value,
                    f"Enrichment readback mismatch: {mid}/{field}")
    old_s = {r["paper_id"]: r for r in before["serdes_paper_screenings"]}
    now_s = {r["paper_id"]: r for r in after["serdes_paper_screenings"]}
    require(set(old_s).issubset(now_s), "Existing screening removed")
    for pid, old in old_s.items():
        if pid in targets:
            continue
        for key, value in old.items():
            if pid in carried and key in {"run_id", "updated_at"}:
                continue
            require(canonical_json(now_s[pid][key]) == canonical_json(value),
                    f"Unrelated screening changed: {pid}/{key}")
    for pid in targets:
        require(now_s[pid]["include_in_survey"] == 1, f"Target not included: {pid}")
    def latest_population(tables):
        latest = {}
        for run in tables["serdes_screening_runs"]:
            if run["status"] == "complete" and run["scope_name"].startswith("all"):
                latest[run["venue"]] = max(run["id"], latest.get(run["venue"], 0))
        return {row["paper_id"] for row in tables["serdes_paper_screenings"]
                if row["run_id"] == latest.get(row["venue"])}
    require(latest_population(after) == latest_population(before) | targets,
            "Latest screening population changed outside approved scope")
    expected_points = [(target, point) for target in plan["targets"] for point in target["new_points"]]
    for target, point in expected_points:
        target_impls = {link["implementation_id"] for link in after["serdes_implementation_papers"]
                        if link["paper_id"] == target["paper_id"]}
        matches = [now_m[mid] for mid in new_mids
                   if now_m[mid]["operating_point_key"] == point["values"]["operating_point_key"]
                   and now_m[mid]["implementation_id"] in target_impls]
        require(len(matches) == 1, "New operating point readback missing or duplicated")
        row = matches[0]
        for field, value in point["values"].items():
            if field == "implementation_id":
                continue  # Assigned transactionally; checked through paper linkage.
            if field == "overall_confidence":
                require(Decimal(str(row[field])) == Decimal(str(value)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP),
                        "Confidence readback mismatch")
            elif field in NUMERIC_FIELDS and value is not None:
                # SQL DECIMAL precision is the schema's explicit storage limit.
                precision = {"process_nm": 3, "channel_loss_db": 4,
                             "energy_pj_bit": 9, "energy_loss_normalized_pj_bit_db": 9}
                if field == "ber":
                    require(abs(float(row[field]) - float(value)) <= abs(float(value)) * 1e-12,
                            f"New BER readback mismatch: {row['id']}")
                else:
                    digits = precision.get(field, 0 if field == "lane_count" else 6)
                    require(Decimal(str(row[field])) == Decimal(str(value)).quantize(Decimal(10) ** -digits, rounding=ROUND_HALF_UP),
                            f"New numeric readback mismatch: {row['id']}/{field}")
            else:
                require(row[field] == value, f"New metadata readback mismatch: {row['id']}/{field}")
        require(any(link["paper_id"] == target["paper_id"]
                    and link["implementation_id"] == row["implementation_id"]
                    for link in after["serdes_implementation_papers"]),
                "New operating point lacks target paper association")
    for table in ("serdes_implementation_families", "serdes_implementation_family_members",
                  "serdes_implementation_match_candidates", "serdes_implementation_match_decision_events",
                  "serdes_measurement_scope_overrides", "serdes_paper_subtype_overrides",
                  "serdes_paper_bibliography"):
        require(fingerprint(before[table]) == fingerprint(after[table]),
                f"Unrelated table changed: {table}")
    old_scopes = {r["measurement_id"]: r for r in before["serdes_measurement_metric_scopes"]}
    new_scopes = {r["measurement_id"]: r for r in after["serdes_measurement_metric_scopes"]}
    for mid, old in old_scopes.items():
        require(canonical_json(old) == canonical_json(new_scopes.get(mid)),
                f"Existing energy scope changed: {mid}")
    for table in ("serdes_implementations", "serdes_implementation_papers", "serdes_measurement_evidence"):
        old_rows = {canonical_json(r) for r in before[table]}
        now_rows = {canonical_json(r) for r in after[table]}
        require(old_rows.issubset(now_rows), f"Existing row modified: {table}")
    for table in ("serdes_paper_link_media", "serdes_paper_link_subtypes"):
        before_rows = {r["paper_id"]: r for r in before[table]}
        after_rows = {r["paper_id"]: r for r in after[table]}
        for pid, row in before_rows.items():
            require(canonical_json(row) == canonical_json(after_rows.get(pid)),
                    f"Existing medium/subtype changed: {pid}")


def execute(conn, plan, original, apply=False):
    receipt = {"version": VERSION, "applied": apply, "new_measurement_ids": [],
               "new_implementation_ids": [], "run_ids": [], "carried_paper_ids": [],
               "counts": Counter()}
    with conn.cursor() as cur:
        if apply:
            cur.execute("SELECT GET_LOCK('serdes_measured_review',10) acquired")
            require(cur.fetchone()["acquired"] == 1, "Survey apply lock unavailable")
        else:
            cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
        before = read_tables(cur, original["tables"], lock=apply)
        for name, rows in before.items():
            require(fingerprint(rows) == plan["before_fingerprints"][name],
                    f"Database changed since extraction: {name}")
        if not apply:
            conn.rollback()
            receipt["counts"] = plan["summary"]
            return receipt
        now = datetime.now()
        seen = {r["evidence_key"] for r in before["serdes_measurement_evidence"]}
        existing_screens = {r["paper_id"]: r for r in before["serdes_paper_screenings"]}
        latest = {}
        for run in before["serdes_screening_runs"]:
            if run["status"] == "complete" and run["scope_name"].startswith("all"):
                latest[run["venue"]] = max(run["id"], latest.get(run["venue"], 0))
        grouped = {}
        for target in plan["targets"]:
            grouped.setdefault(target["screening"]["venue"], []).append(target)
        for venue, targets in grouped.items():
            population = {pid: row for pid, row in existing_screens.items()
                          if row["venue"] == venue and row["run_id"] == latest.get(venue)}
            for target in targets:
                population[target["paper_id"]] = target["screening"]
            counts = Counter(r["relevance_class"] for r in population.values())
            run = {"venue": venue, "scope_name": "all_measured_review", "scope_version": VERSION,
                   "target_count": len(population), "core_count": counts["core"],
                   "adjacent_count": counts["adjacent"], "review_count": counts["needs_review"],
                   "excluded_count": counts["out_of_scope"],
                   "abstract_count": sum(r.get("abstract_id") is not None for r in population.values()),
                   "status": "complete", "started_at": now, "completed_at": now}
            rid = insert_row(cur, "serdes_screening_runs", run, tuple(run))
            receipt["run_ids"].append(rid)
            target_ids = {t["paper_id"] for t in targets}
            carried = sorted(set(population) - target_ids)
            receipt["carried_paper_ids"].extend(carried)
            for pid in carried:
                cur.execute("UPDATE serdes_paper_screenings SET run_id=%s WHERE paper_id=%s", (rid, pid))
            for target in targets:
                screen = {**target["screening"], "run_id": rid, "screened_at": now}
                if target["paper_id"] in existing_screens:
                    columns = [c for c in SCREEN_COLUMNS if c != "paper_id"]
                    cur.execute("UPDATE serdes_paper_screenings SET "
                                + ",".join(f"{c}=%s" for c in columns) + " WHERE paper_id=%s",
                                tuple(screen[c] for c in columns) + (target["paper_id"],))
                else:
                    insert_row(cur, "serdes_paper_screenings", screen, SCREEN_COLUMNS)
                receipt["counts"]["papers_included"] += 1
        impl_by_key = {r["implementation_key"]: r["id"] for r in before["serdes_implementations"]}
        for target in plan["targets"]:
            pid = target["paper_id"]
            iid = target.get("implementation_id")
            if target["new_points"] and iid is None:
                implementation = target["implementation"]
                require(implementation["implementation_key"] not in impl_by_key,
                        "New implementation key already exists")
                iid = insert_row(cur, "serdes_implementations", implementation, tuple(implementation))
                insert_row(cur, "serdes_implementation_papers",
                           {"implementation_id": iid, "paper_id": pid, "relation_type": "primary"},
                           ("implementation_id", "paper_id", "relation_type"))
                receipt["new_implementation_ids"].append(iid)
                impl_by_key[implementation["implementation_key"]] = iid
            for update in target["updates"]:
                fields = update["fields"]
                cur.execute("UPDATE serdes_measurements SET " + ",".join(f"{f}=%s" for f in fields)
                            + " WHERE id=%s", tuple(fields.values()) + (update["measurement_id"],))
                receipt["counts"]["existing_numeric_fields_filled"] += sum(f in NUMERIC_FIELDS for f in fields)
                receipt["counts"]["existing_measurements_enriched"] += 1
            for point in target["new_points"]:
                values = {**point["values"], "implementation_id": iid}
                mid = insert_row(cur, "serdes_measurements", values, MEASUREMENT_COLUMNS)
                receipt["new_measurement_ids"].append(mid)
                receipt["counts"]["new_pdf_points"] += 1
                for evidence in point["evidence"]:
                    receipt["counts"]["evidence_added"] += store_evidence(cur, evidence, mid, seen)
                scope = {"measurement_id": mid,
                         "energy_component_scope": point.get("energy_component_scope", "unknown"),
                         "scope_source": "pdf_review", "scope_confidence": point.get("scope_confidence", 0.90),
                         "reason_codes": json.dumps(point.get("qualifiers", []), ensure_ascii=False),
                         "evidence_text": point.get("scope_evidence", ""),
                         "classifier_version": VERSION, "classified_at": now}
                insert_row(cur, "serdes_measurement_metric_scopes", scope, tuple(scope))
            for evidence in target["evidence"]:
                receipt["counts"]["evidence_added"] += store_evidence(
                    cur, evidence, evidence["measurement_id"], seen)
            for table, values in target.get("new_classifications", {}).items():
                values = {**values, "classified_at": now}
                insert_row(cur, table, values, tuple(values))
                receipt["counts"][table + "_added"] += 1
        after = read_tables(cur, original["tables"])
        assert_post_state(before, after, plan, receipt)
        receipt["after_fingerprints"] = {name: fingerprint(rows) for name, rows in after.items()}
        receipt["counts"]["new_implementations"] = len(receipt["new_implementation_ids"])
        receipt["counts"]["venues_updated"] = len(receipt["run_ids"])
        receipt["counts"]["existing_population_carried"] = len(receipt["carried_paper_ids"])
        conn.commit()
        cur.execute("SELECT RELEASE_LOCK('serdes_measured_review')")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    data = args.plan.read_bytes()
    plan = json.loads(data)
    original_path, qualified_path = Path(plan["snapshot_path"]), Path(plan["qualified_path"])
    require(digest(original_path.read_bytes()) == plan["snapshot_sha256"], "Snapshot file changed")
    require(digest(qualified_path.read_bytes()) == plan["qualified_sha256"], "Qualification file changed")
    original = json.loads(original_path.read_bytes())
    qualified = json.loads(qualified_path.read_bytes())
    validate_plan(plan, original, qualified)
    if args.apply:
        for source in plan["sources"]:
            require(digest(Path(source["path"]).read_bytes()) == source["sha256"],
                    "Source PDF changed: " + source["path"])
    conn = connect()
    try:
        # A repeated apply is read-only if the original receipt matches both the
        # plan and the complete post-transaction database fingerprints.
        if args.apply and args.report.exists():
            previous = json.loads(args.report.read_bytes())
            if previous.get("applied") and previous.get("plan_sha256") == digest(data):
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
                    state = read_tables(cur, original["tables"])
                    require(all(fingerprint(rows) == previous["after_fingerprints"][table]
                                for table, rows in state.items()), "Database differs from previous receipt")
                    conn.rollback()
                print(json.dumps({"already_applied": True, "counts": previous["counts"]}))
                return
        receipt = execute(conn, plan, original, apply=args.apply)
        receipt["plan_sha256"] = digest(data)
        receipt["snapshot_sha256"] = plan["snapshot_sha256"]
        receipt["qualified_sha256"] = plan["qualified_sha256"]
        receipt["completed_at"] = datetime.now().isoformat()
        save(args.report, receipt)
        print(json.dumps({"applied": receipt["applied"], "counts": receipt["counts"],
                          "report": str(args.report)}, ensure_ascii=False))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
