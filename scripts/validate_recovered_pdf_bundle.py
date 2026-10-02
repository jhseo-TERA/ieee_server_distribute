#!/usr/bin/env python3
"""Structurally validate every project PDF listed by a Drive restore manifest."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.download_guard import validate_pdf_file  # noqa: E402

logging.getLogger("pypdf").setLevel(logging.ERROR)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Restore manifest; defaults to the newest drive-zip-extracted manifest.",
    )
    args = parser.parse_args()

    manifest_path = args.manifest
    if manifest_path is None:
        candidates = sorted((ROOT / "drive-zip-extracted").glob("RESTORE_MANIFEST_*.json"))
        if not candidates:
            parser.error("no restore manifest found")
        manifest_path = candidates[-1]
    manifest_path = manifest_path.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    valid: list[str] = []
    invalid: list[dict[str, str]] = []
    skipped_non_pdf = 0
    project_entries = [
        item for item in manifest.get("files", []) if item.get("project_status")
    ]
    for index, item in enumerate(project_entries, start=1):
        relative = str(item["path"])
        path = ROOT / Path(*relative.split("/"))
        ok, reason = validate_pdf_file(path)
        if ok:
            valid.append(relative)
        else:
            invalid.append({"path": relative, "reason": str(reason or "invalid PDF")})
        if index % 100 == 0 or index == len(project_entries):
            print(f"Validated {index}/{len(project_entries)}", flush=True)

    skipped_non_pdf = len(manifest.get("files", [])) - len(project_entries)
    report = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_manifest": str(manifest_path),
        "project_entries": len(project_entries),
        "valid_count": len(valid),
        "invalid_count": len(invalid),
        "skipped_non_pdf_count": skipped_non_pdf,
        "valid_paths": valid,
        "invalid": invalid,
    }
    report_path = manifest_path.with_name(
        "PDF_VALIDATION_" + time.strftime("%Y%m%d_%H%M%S") + ".json"
    )
    with report_path.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    print(json.dumps({key: report[key] for key in (
        "project_entries", "valid_count", "invalid_count", "skipped_non_pdf_count"
    )}, ensure_ascii=False, indent=2))
    for finding in invalid:
        print(f"INVALID: {finding['path']}: {finding['reason']}")
    print(f"Report: {report_path}")
    return 1 if invalid else 0


if __name__ == "__main__":
    raise SystemExit(main())
