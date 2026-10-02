# -*- coding: utf-8 -*-
"""Export SQL-backed SerDes manual-review and FoM data to JSON for XLSX authoring."""

from __future__ import annotations

import argparse
from collections import Counter, deque
from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path
import re
from typing import Any

try:
    from .serdes_data_pipeline import db_connect, ensure_schema
except ImportError:  # Direct script execution keeps scripts/ on sys.path.
    from serdes_data_pipeline import db_connect, ensure_schema
from serdes_metrics import canonical_ieee_url, plain_text


ROOT = Path(__file__).resolve().parents[1]
LATEST_RUNS_SQL = (
    "SELECT MAX(id) FROM serdes_screening_runs "
    "WHERE status='complete' AND scope_name LIKE 'all%' GROUP BY venue"
)
SOURCE_PRIORITY = {
    "manual": 60,
    "user_sheet": 55,
    "pdf": 50,
    "reference_xlsx": 40,
    "abstract": 30,
    "title": 10,
}
REVIEW_PRIORITY = {
    "verified": 4,
    "reviewed": 3,
    "curated_reference": 2,
    "extracted": 1,
}


def json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def first_author(authors: str | None) -> str:
    value = plain_text(authors)
    if not value:
        return ""
    if ";" in value:
        return value.split(";", 1)[0].strip()
    # IEEE metadata normally separates full author names with commas and may
    # put "and" only before the final author.  Split the list delimiter before
    # checking "and" so the whole pre-final author list is not returned.
    if "," in value:
        return value.split(",", 1)[0].strip()
    if " and " in value.lower():
        return re.split(r"\s+and\s+", value, maxsplit=1, flags=re.I)[0].strip()
    return value.strip()


def reason_codes(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return value
        if isinstance(parsed, list):
            return ", ".join(str(item) for item in parsed)
    return plain_text(value)


def weighted_interleave(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Spread venues across days while preserving priority within each venue."""
    grouped: dict[str, deque] = {}
    for row in sorted(
        rows,
        key=lambda item: (
            item.get("venue") or "",
            -float(item.get("relevance_score") or 0),
            -int(item.get("year") or 0),
            int(item.get("paper_id") or 0),
        ),
    ):
        grouped.setdefault(row["venue"], deque()).append(row)
    totals = {venue: len(queue) for venue, queue in grouped.items()}
    assigned = Counter()
    ordered: list[dict[str, Any]] = []
    while grouped:
        venue = min(
            grouped,
            key=lambda name: (
                assigned[name] / totals[name],
                -float(grouped[name][0].get("relevance_score") or 0),
                name,
            ),
        )
        ordered.append(grouped[venue].popleft())
        assigned[venue] += 1
        if not grouped[venue]:
            del grouped[venue]
    for index, row in enumerate(ordered, 1):
        row["sequence"] = index
        row["batch_no"] = (index - 1) // 50 + 1
        row["batch_item"] = (index - 1) % 50 + 1
    return ordered


def measurement_rank(row: dict[str, Any]) -> tuple:
    populated = sum(
        row.get(field) is not None
        for field in (
            "lane_rate_gbps", "aggregate_rate_gbps", "reported_rate_gbps",
            "symbol_rate_gbaud", "energy_pj_bit", "power_mw", "process_nm",
            "channel_loss_db", "ber", "active_area_mm2",
        )
    )
    return (
        REVIEW_PRIORITY.get(row.get("measurement_review_status"), 0),
        SOURCE_PRIORITY.get(row.get("source_kind"), 0),
        populated,
        float(row.get("overall_confidence") or 0),
        int(row.get("evidence_count") or 0),
        row.get("measurement_id") or 0,
    )


def load_review_rows(conn) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT p.id paper_id, p.article_number, p.title, p.authors, p.year,
                   p.source_name venue, p.source_type, p.issue, p.doi,
                   p.citation_count, p.pdf_available, p.pdf_local_path,
                   s.run_id, s.scope_version, s.relevance_score,
                   s.reason_codes, s.rationale, s.screening_source,
                   a.id abstract_id, a.provider abstract_provider,
                   a.source_url abstract_source_url, a.abstract_text,
                   a.content_sha256 abstract_sha256, a.retrieved_at
            FROM serdes_paper_screenings s
            JOIN papers p ON p.id=s.paper_id
            LEFT JOIN paper_current_abstracts a ON a.paper_id=p.id
            WHERE s.relevance_class='needs_review'
              AND s.run_id IN ({LATEST_RUNS_SQL})
            ORDER BY s.relevance_score DESC, CAST(p.year AS UNSIGNED) DESC,
                     p.source_name, p.id
            """
        )
        rows = list(cur.fetchall())
    normalized = []
    for raw in rows:
        row = {key: json_value(value) for key, value in raw.items()}
        row["year"] = int(row["year"]) if str(row.get("year") or "").isdigit() else None
        row["first_author"] = first_author(row.get("authors"))
        row["reason_codes_text"] = reason_codes(row.get("reason_codes"))
        row["ieee_url"] = canonical_ieee_url(row.get("article_number"))
        row["abstract_url"] = (
            f"https://ieeexplore.ieee.org/abstract/document/{row['article_number']}"
        )
        row["abstract_status"] = "db_cached" if row.get("abstract_text") else "manual_open"
        normalized.append(row)
    return weighted_interleave(normalized)


def load_metric_rows(conn) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT p.id paper_id, p.article_number, p.title, p.authors, p.year,
                   p.source_name venue, p.doi, p.pdf_available,
                   s.relevance_class, s.relevance_score,
                   a.provider abstract_provider, a.source_url abstract_source_url,
                   a.retrieved_at abstract_retrieved_at,
                   m.id measurement_id, m.source_kind, m.review_status measurement_review_status,
                   m.overall_confidence, m.reported_rate_text,
                   m.reported_rate_gbps, m.reported_rate_min_gbps,
                   m.reported_rate_max_gbps, m.rate_scope, m.lane_rate_gbps,
                   m.lane_count, m.aggregate_rate_gbps, m.aggregate_rate_basis,
                   m.symbol_rate_gbaud, m.throughput_density_gbps_per_mm,
                   m.power_mw, m.power_scope, m.energy_pj_bit, m.energy_scope,
                   m.energy_basis, m.energy_loss_normalized_pj_bit_db,
                   m.channel_loss_db, m.loss_frequency_ghz, m.ber, m.ber_scope,
                   m.active_area_mm2, m.process_text, m.process_nm,
                   m.link_class, m.component_scope, m.modulation,
                   COALESCE(ec.evidence_count, 0) evidence_count
            FROM serdes_paper_screenings s
            JOIN papers p ON p.id=s.paper_id
            LEFT JOIN paper_current_abstracts a ON a.paper_id=p.id
            LEFT JOIN serdes_implementation_papers ip ON ip.paper_id=p.id
            LEFT JOIN serdes_measurements m ON m.implementation_id=ip.implementation_id
            LEFT JOIN (
                SELECT measurement_id, COUNT(*) evidence_count
                FROM serdes_measurement_evidence GROUP BY measurement_id
            ) ec ON ec.measurement_id=m.id
            WHERE s.include_in_survey=1
              AND s.run_id IN ({LATEST_RUNS_SQL})
            ORDER BY p.id, m.id
            """
        )
        raw_rows = list(cur.fetchall())
    papers: dict[int, dict[str, Any]] = {}
    best: dict[int, dict[str, Any]] = {}
    for raw in raw_rows:
        row = {key: json_value(value) for key, value in raw.items()}
        paper_id = int(row["paper_id"])
        if paper_id not in papers:
            papers[paper_id] = {
                key: row.get(key)
                for key in (
                    "paper_id", "article_number", "title", "authors", "year",
                    "venue", "doi", "pdf_available", "relevance_class",
                    "relevance_score", "abstract_provider", "abstract_source_url",
                    "abstract_retrieved_at",
                )
            }
        if (
            row.get("measurement_id") is not None
            and row.get("measurement_review_status") not in {"rejected", "invalid"}
        ):
            if paper_id not in best or measurement_rank(row) > measurement_rank(best[paper_id]):
                best[paper_id] = row

    output = []
    for paper_id, paper in papers.items():
        row = dict(paper)
        row["year"] = int(row["year"]) if str(row.get("year") or "").isdigit() else None
        row["first_author"] = first_author(row.get("authors"))
        row["ieee_url"] = canonical_ieee_url(row.get("article_number"))
        row["abstract_url"] = (
            f"https://ieeexplore.ieee.org/abstract/document/{row['article_number']}"
        )
        measurement = best.get(paper_id)
        if measurement:
            for key, value in measurement.items():
                if key not in row:
                    row[key] = value
        source_kind = row.get("source_kind")
        row["estimate_status"] = {
            "manual": "verified_manual",
            "user_sheet": "verified_user_sheet",
            "pdf": "verified_pdf",
            "reference_xlsx": "curated_reference",
            "abstract": "abstract_estimate",
            "title": "title_clue",
        }.get(source_kind, "missing")
        output.append(row)
    output.sort(
        key=lambda item: (
            -int(item.get("year") or 0),
            item.get("venue") or "",
            item.get("title") or "",
        )
    )
    return output


def summarize(review_rows: list[dict[str, Any]], metric_rows: list[dict[str, Any]]) -> dict:
    review_by_venue = Counter(row["venue"] for row in review_rows)
    review_with_abstract = sum(bool(row.get("abstract_text")) for row in review_rows)
    metric_by_provider = Counter(
        row.get("abstract_provider") or "missing" for row in metric_rows
    )
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "screening_version": "serdes-screen-2.3",
        "review_total": len(review_rows),
        "review_batches": max((row["batch_no"] for row in review_rows), default=0),
        "review_with_abstract": review_with_abstract,
        "review_missing_abstract": len(review_rows) - review_with_abstract,
        "review_by_venue": dict(sorted(review_by_venue.items(), key=lambda item: (-item[1], item[0]))),
        "included_total": len(metric_rows),
        "included_abstract_by_provider": dict(metric_by_provider),
        "included_with_rate": sum(
            any(row.get(field) is not None for field in (
                "lane_rate_gbps", "aggregate_rate_gbps", "reported_rate_gbps",
                "symbol_rate_gbaud",
            ))
            for row in metric_rows
        ),
        "included_with_energy": sum(row.get("energy_pj_bit") is not None for row in metric_rows),
        "included_with_process": sum(row.get("process_nm") is not None for row in metric_rows),
        "included_with_loss": sum(row.get("channel_loss_db") is not None for row in metric_rows),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    output = args.output
    if not output.is_absolute():
        output = ROOT / output
    conn = db_connect()
    try:
        ensure_schema(conn)
        review_rows = load_review_rows(conn)
        metric_rows = load_metric_rows(conn)
    finally:
        conn.close()
    payload = {
        "summary": summarize(review_rows, metric_rows),
        "review_rows": review_rows,
        "metric_rows": metric_rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=json_value),
        encoding="utf-8",
    )
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    print(f"Snapshot: {output}")


if __name__ == "__main__":
    main()
