"""Reversibly quarantine unreferenced JLT aliases identical to canonical PDFs."""
import argparse
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

import pymysql

try:
    from . import scan_pdf_integrity as inventory
except ImportError:
    import scan_pdf_integrity as inventory


def move_duplicates(report, references, root, destination_root):
    """Only move an unreferenced URI alias with a valid referenced DOI copy."""
    root = root.resolve()
    destination_root = destination_root.resolve()
    destination_root.relative_to(root / "pdf-quarantine")
    manifest = []
    try:
        for item in report["duplicates"]:
            source = (root / item["original_path"]).resolve()
            retained = (root / item["retained_path"]).resolve()
            if (source.parent != root / "optica-pdf" or retained.parent != root / "optica-pdf"
                    or not re.fullmatch(r"jlt-\d+(?:-\d+)+\.pdf", source.name)
                    or not retained.name.startswith("jlt-doi-")
                    or inventory.path_key(source) in references
                    or inventory.path_key(retained) not in references):
                continue
            if inventory.sha256(source) != item["sha256"] or inventory.sha256(retained) != item["sha256"]:
                raise RuntimeError("Duplicate contents changed after inventory")
            if not inventory.validate_pdf_file(retained)[0]:
                raise RuntimeError("Canonical PDF did not pass structural validation")
            destination = destination_root / source.name
            if destination.exists():
                raise RuntimeError("Quarantine destination already exists")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            manifest.append({**item, "quarantine_path": destination.relative_to(root).as_posix()})
    except BaseException:
        for item in reversed(manifest):
            shutil.move(str(root / item["quarantine_path"]), str(root / item["original_path"]))
        raise
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--external-references-verified", action="store_true",
                        help="Confirm Zotero/Obsidian no longer reference the legacy paths")
    args = parser.parse_args()
    if args.apply and not args.external_references_verified:
        parser.error("Before --apply, verify Zotero/Obsidian references and pass "
                     "--external-references-verified; MySQL references alone are insufficient")
    report = json.loads(args.report.read_text(encoding="utf-8"))
    if not args.apply:
        print(json.dumps(report.get("duplicates", []), ensure_ascii=False, indent=2))
        return 0
    inventory.STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = inventory.STATE_DIR / "routine.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(descriptor)
    connection = None
    try:
        connection = pymysql.connect(**inventory.DB)
        refs = {inventory.path_key(p) for row in inventory.fetch_records(connection)
                if (p := inventory.safe_database_path(row["pdf_local_path"])) is not None}
        destination = inventory.QUARANTINE_DIR / "duplicates" / datetime.now().strftime("%Y%m%d_%H%M%S")
        destination.mkdir(parents=True, exist_ok=False)
        (destination / "plan.json").write_text(json.dumps(report["duplicates"], ensure_ascii=False, indent=2), encoding="utf-8")
        manifest = move_duplicates(report, refs, inventory.ROOT, destination)
        (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Quarantined {len(manifest)} duplicate aliases; recoverable at {destination}")
        return 0
    finally:
        if connection is not None:
            connection.close()
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
