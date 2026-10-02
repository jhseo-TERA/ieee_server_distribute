#!/usr/bin/env python
"""Export a read-only SerDes metric review queue as JSON and CSV raw data."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.serdes_data_pipeline import db_connect  # noqa: E402
from serdes_review_queue import build_review_package, json_value  # noqa: E402


LATEST_RUNS_SQL = (
    "SELECT MAX(id) FROM serdes_screening_runs "
    "WHERE status='complete' AND scope_name LIKE 'all%' GROUP BY venue"
)


MEASUREMENT_SQL = f"""
SELECT
    m.id measurement_id, m.implementation_id, m.operating_point_key,
    m.source_kind, m.review_status, m.overall_confidence, m.updated_at,
    m.reported_rate_text, m.reported_rate_gbps, m.reported_rate_min_gbps,
    m.reported_rate_max_gbps, m.rate_scope, m.lane_rate_gbps, m.lane_count,
    m.aggregate_rate_gbps, m.aggregate_rate_basis, m.symbol_rate_gbaud,
    m.throughput_density_gbps_per_mm, m.power_mw, m.power_scope,
    m.energy_pj_bit, m.energy_scope, m.energy_basis,
    m.energy_loss_normalized_pj_bit_db, m.process_text, m.process_nm,
    m.channel_loss_db, m.loss_frequency_ghz, m.ber, m.ber_scope,
    m.active_area_mm2, m.link_class, m.component_scope, m.modulation,
    p.id paper_id, p.article_number, p.doi, p.title, p.authors, p.year,
    p.source_name venue, p.citation_count, p.pdf_available, p.url paper_url,
    s.relevance_class, a.abstract_text paper_abstract,
    COALESCE(subtype_override.link_medium, medium.link_medium) link_medium,
    CASE WHEN subtype_override.paper_id IS NOT NULL
         THEN subtype_override.override_source ELSE medium.medium_source END medium_source,
    CASE WHEN subtype_override.paper_id IS NOT NULL
         THEN 1.0 ELSE medium.medium_confidence END medium_confidence,
    COALESCE(subtype_override.link_subtype, subtype.link_subtype) link_subtype,
    CASE WHEN subtype_override.paper_id IS NOT NULL
         THEN subtype_override.override_source ELSE subtype.subtype_source END subtype_source,
    CASE WHEN subtype_override.paper_id IS NOT NULL
         THEN 1.0 ELSE subtype.subtype_confidence END subtype_confidence,
    COALESCE(scope_override.energy_component_scope,
             metric_scope.energy_component_scope) energy_component_scope,
    CASE WHEN scope_override.measurement_id IS NOT NULL
         THEN scope_override.override_source ELSE metric_scope.scope_source END scope_source,
    CASE WHEN scope_override.measurement_id IS NOT NULL
         THEN 1.0 ELSE metric_scope.scope_confidence END scope_confidence,
    CASE WHEN scope_override.measurement_id IS NOT NULL
         THEN scope_override.reason ELSE metric_scope.reason_codes END scope_reason_codes,
    COALESCE(ec.evidence_count, 0) evidence_count
FROM serdes_measurements m
JOIN serdes_implementations i ON i.id=m.implementation_id
JOIN papers p ON p.id=i.canonical_paper_id
JOIN serdes_paper_screenings s ON s.paper_id=p.id
  AND s.run_id IN ({LATEST_RUNS_SQL})
LEFT JOIN paper_current_abstracts a ON a.paper_id=p.id AND a.is_current=1
LEFT JOIN serdes_paper_link_media medium ON medium.paper_id=p.id
LEFT JOIN serdes_paper_link_subtypes subtype ON subtype.paper_id=p.id
LEFT JOIN serdes_paper_subtype_overrides subtype_override
  ON subtype_override.paper_id=p.id
 AND subtype_override.review_status IN ('verified','reviewed','approved','active')
LEFT JOIN serdes_measurement_metric_scopes metric_scope
  ON metric_scope.measurement_id=m.id
LEFT JOIN serdes_measurement_scope_overrides scope_override
  ON scope_override.measurement_id=m.id
 AND scope_override.review_status IN ('verified','reviewed','approved','active')
LEFT JOIN (
    SELECT measurement_id, COUNT(*) evidence_count
    FROM serdes_measurement_evidence GROUP BY measurement_id
) ec ON ec.measurement_id=m.id
WHERE p.source_system='ieee' AND s.include_in_survey=1
  AND m.source_kind<>'title'
  AND m.review_status NOT IN ('rejected','invalid','needs_review')
ORDER BY m.id
"""


EVIDENCE_SQL = f"""
SELECT DISTINCT
    e.id evidence_id, e.measurement_id, e.field_name, e.source_kind,
    e.source_url, e.source_locator, e.evidence_text, e.extraction_method,
    e.extractor_version, e.confidence, e.review_status, e.updated_at
FROM serdes_measurement_evidence e
JOIN serdes_measurements m ON m.id=e.measurement_id
JOIN serdes_implementations i ON i.id=m.implementation_id
JOIN papers p ON p.id=i.canonical_paper_id
JOIN serdes_paper_screenings s ON s.paper_id=p.id
  AND s.run_id IN ({LATEST_RUNS_SQL})
WHERE p.source_system='ieee' AND s.include_in_survey=1
  AND m.source_kind<>'title'
  AND m.review_status NOT IN ('rejected','invalid','needs_review')
ORDER BY e.measurement_id, e.id
"""


DUPLICATE_CANDIDATE_SQL = """
SELECT c.id candidate_id, c.candidate_key, c.candidate_tier,
       c.decision_status, c.decision_source, c.match_score,
       c.title_similarity, c.title_token_jaccard, c.shared_author_count,
       c.author_jaccard, c.first_author_match, c.year_gap,
       c.technical_matches, c.technical_conflicts, c.reason_codes,
       c.classifier_version, c.decision_note, c.decided_by, c.decided_at,
       c.family_id,
       cp.id conference_paper_id, cp.article_number conference_article_number,
       cp.source_name conference_venue, cp.year conference_year,
       cp.title conference_title, cp.authors conference_authors,
       CONCAT('https://ieeexplore.ieee.org/document/', cp.article_number, '/') conference_url,
       jp.id journal_paper_id, jp.article_number journal_article_number,
       jp.source_name journal_venue, jp.year journal_year,
       jp.title journal_title, jp.authors journal_authors,
       CONCAT('https://ieeexplore.ieee.org/document/', jp.article_number, '/') journal_url
FROM serdes_implementation_match_candidates c
JOIN papers cp ON cp.id=c.conference_paper_id
JOIN papers jp ON jp.id=c.journal_paper_id
ORDER BY FIELD(c.decision_status,'manual_review','approved','auto_grouped','rejected'),
         c.match_score DESC, c.id
"""


def load_rows(conn) -> tuple[list[dict], list[dict], list[dict]]:
    """Read the fixed included corpus without mutating database state."""
    with conn.cursor() as cur:
        cur.execute(MEASUREMENT_SQL)
        measurements = list(cur.fetchall())
        cur.execute(EVIDENCE_SQL)
        evidence = list(cur.fetchall())
        cur.execute(DUPLICATE_CANDIDATE_SQL)
        duplicate_candidates = list(cur.fetchall())
    return measurements, evidence, duplicate_candidates


def csv_cell(value: Any) -> Any:
    value = json_value(value)
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if value is None:
        return ""
    # CSVs are intentionally Excel-friendly.  Neutralize formula-like text
    # while leaving numeric values untouched; the JSON package preserves the
    # exact original string for machine re-import.
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = []
    seen = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fields.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        if not fields:
            return
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_cell(row.get(key)) for key in fields})


def summary_rows(summary: dict) -> list[dict]:
    rows = []
    for key, value in summary.items():
        if isinstance(value, dict):
            rows.extend(
                {"metric": f"{key}.{child}", "value": child_value}
                for child, child_value in value.items()
            )
        else:
            rows.append({"metric": key, "value": value})
    return rows


def write_package(package: dict, output_dir: Path, prefix: str) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": output_dir / f"{prefix}.json",
        "review_queue_csv": output_dir / f"{prefix}_review_queue.csv",
        "measurement_flags_csv": output_dir / f"{prefix}_measurement_flags.csv",
        "summary_csv": output_dir / f"{prefix}_summary.csv",
        "rules_csv": output_dir / f"{prefix}_rules.csv",
        "data_dictionary_csv": output_dir / f"{prefix}_data_dictionary.csv",
        "duplicate_candidates_csv": output_dir / f"{prefix}_duplicate_candidates.csv",
    }
    paths["json"].write_text(
        json.dumps(package, ensure_ascii=False, indent=2, default=json_value),
        encoding="utf-8",
    )
    write_csv(paths["review_queue_csv"], package["review_queue"])
    write_csv(paths["measurement_flags_csv"], package["measurement_flags"])
    write_csv(paths["summary_csv"], summary_rows(package["summary"]))
    write_csv(paths["rules_csv"], package["rules"])
    write_csv(paths["data_dictionary_csv"], package["data_dictionary"])
    # Duplicate-family review data is an optional extension so callers that
    # build a review package without a live database can still export the
    # core queue deterministically.
    write_csv(paths["duplicate_candidates_csv"], package.get("duplicate_candidates", []))
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path,
        default=ROOT / "outputs" / "serdes_reference",
    )
    parser.add_argument("--prefix", default="serdes_review_queue")
    parser.add_argument("--batch-size", type=int, default=50)
    args = parser.parse_args()
    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")
    output_dir = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir

    conn = db_connect()
    try:
        measurements, evidence, duplicate_candidates = load_rows(conn)
    finally:
        conn.close()
    package = build_review_package(
        measurements, evidence, batch_size=args.batch_size,
        generated_at=datetime.now().isoformat(timespec="seconds"),
    )
    package["duplicate_candidates"] = [
        {key: json_value(value) for key, value in row.items()}
        for row in duplicate_candidates
    ]
    package["summary"]["duplicate_candidates"] = len(duplicate_candidates)
    package["summary"]["duplicate_manual_review"] = sum(
        row.get("decision_status") == "manual_review"
        for row in package["duplicate_candidates"]
    )
    package["summary"]["implementation_families"] = len({
        row.get("family_id") for row in package["duplicate_candidates"]
        if row.get("family_id") is not None
    })
    paths = write_package(package, output_dir, args.prefix)
    print(json.dumps(package["summary"], ensure_ascii=False, indent=2))
    for label, path in paths.items():
        print(f"{label}: {path}")


if __name__ == "__main__":
    main()
