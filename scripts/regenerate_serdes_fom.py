# -*- coding: utf-8 -*-
r"""Audit or apply spreadsheet-style SerDes FoM regeneration.

Examples:
  .venv\Scripts\python.exe scripts\regenerate_serdes_fom.py
  .venv\Scripts\python.exe scripts\regenerate_serdes_fom.py --apply

The default is a read-only dry run.  ``--apply`` fills only missing calculated
energy values and writes traceable formula evidence; reported values are never
overwritten.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from serdes_fom import json_default, regenerate_fom  # noqa: E402
from scripts.serdes_data_pipeline import db_connect, ensure_schema  # noqa: E402


DEFAULT_OUTPUT_DIR = ROOT / "outputs" / "serdes_reference"
CSV_FIELDS = (
    "measurement_id", "paper_id", "article_number", "title", "year", "venue",
    "url", "source_kind", "review_status", "operating_point_key", "action",
    "reason", "power_mw", "inferred_power_scope", "selected_rate_field",
    "selected_rate_gbps", "denominator_reason", "calculated_energy_pj_bit",
    "existing_energy_pj_bit", "existing_energy_basis", "absolute_delta",
    "relative_delta", "calculation_confidence", "power_evidence_id",
    "rate_evidence_id", "source_locator", "context_overlap_chars",
    "power_evidence_text", "rate_evidence_text",
)
REVIEW_ACTIONS = frozenset({"skip", "review_only", "audit_conflict", "clear_stale"})


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_reports(report: dict, output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = "applied" if report["applied"] else "dry_run"
    json_path = output_dir / f"serdes_fom_regeneration_{suffix}.json"
    all_csv_path = output_dir / f"serdes_fom_regeneration_{suffix}.csv"
    review_csv_path = output_dir / f"serdes_fom_review_{suffix}.csv"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=json_default),
        encoding="utf-8",
    )
    write_csv(all_csv_path, report["decisions"])
    write_csv(
        review_csv_path,
        [item for item in report["decisions"] if item.get("action") in REVIEW_ACTIONS],
    )
    return {
        "json": str(json_path),
        "all_csv": str(all_csv_path),
        "review_csv": str(review_csv_path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Commit calculated values and formula/cross-check evidence",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--min-abstract-overlap", type=int, default=40)
    parser.add_argument("--min-energy", type=float, default=0.001)
    parser.add_argument("--max-energy", type=float, default=1000.0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    output_dir = args.output_dir
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    if args.min_abstract_overlap < 0:
        raise SystemExit("--min-abstract-overlap must be non-negative")
    if args.min_energy <= 0 or args.max_energy <= args.min_energy:
        raise SystemExit("Energy bounds must be positive and increasing")

    conn = db_connect()
    try:
        ensure_schema(conn)
        report = regenerate_fom(
            conn,
            apply=args.apply,
            min_abstract_overlap=args.min_abstract_overlap,
            min_energy_pj_bit=args.min_energy,
            max_energy_pj_bit=args.max_energy,
        )
    finally:
        conn.close()
    paths = write_reports(report, output_dir)
    print(json.dumps({"summary": report["summary"], "reports": paths}, indent=2))


if __name__ == "__main__":
    main()
