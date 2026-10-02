"""Inspect all PDF folders; DesignCon findings require manual restoration.

Automatic sources use recoverable quarantine and a repair queue. DesignCon
files are inspected by their saved paths and are never moved or downloaded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pymysql
from dotenv import load_dotenv

try:
    from .download_guard import queue_repairs, validate_pdf_file, STATE_DIR
except ImportError:
    from download_guard import queue_repairs, validate_pdf_file, STATE_DIR

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs/pdf_integrity"
QUARANTINE_DIR = ROOT / "pdf-quarantine"
PROVIDER_DIRS = {name: ROOT / folder for name, folder in {
    "ieee": "ieee-pdf", "optica": "optica-pdf", "nature": "nature-pdf",
    "designcon": "DesignCon",
}.items()}
AUTO_PROVIDERS = frozenset({"ieee", "optica", "nature"})
load_dotenv(ROOT / ".env")
DB = dict(host=os.getenv("DB_HOST", "127.0.0.1"), port=int(os.getenv("DB_PORT", "3306")),
          user=os.getenv("DB_USER", "root"), password=os.getenv("DB_PASSWORD", ""),
          database=os.getenv("DB_NAME", "ieee_repo"), charset="utf8mb4")


def utcnow():
    return datetime.now(timezone.utc)


def safe_database_path(value):
    if not value:
        return None
    path = (ROOT / str(value).replace("\\", "/")).resolve()
    try:
        path.relative_to(ROOT.resolve())
    except ValueError:
        return None
    return path


def path_key(path):
    return str(path.resolve()).casefold()


def sha256(path):
    with path.open("rb") as handle:
        digest = hashlib.sha256()
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
        return digest.hexdigest()


def fetch_records(connection):
    # Even a cleared availability flag does not make a saved path unreferenced.
    with connection.cursor() as cursor:
        cursor.execute("SELECT article_number,source_system,is_favorite,pdf_available,pdf_local_path "
                       "FROM papers WHERE pdf_local_path IS NOT NULL OR pdf_available=1")
        return [dict(zip(("article_number", "provider", "is_favorite", "pdf_available", "pdf_local_path"), row))
                for row in cursor.fetchall()]


def validate_entry(entry):
    provider, path = entry
    valid, error = validate_pdf_file(path)
    return provider, path, valid, error


def inspect_files(records, workers=4):
    references = defaultdict(list)
    missing = []
    for record in records:
        path = safe_database_path(record["pdf_local_path"])
        if path is not None:
            references[path_key(path)].append(record)
        if record["provider"] in PROVIDER_DIRS and record["pdf_available"] and (
                path is None or not path.is_file()):
            missing.append({**record, "reason": "database_file_missing_or_unsafe"})
    entries = [(provider, p) for provider, directory in PROVIDER_DIRS.items()
               for p in directory.rglob("*.pdf") if p.is_file()]
    damaged, orphans, manual, unmanaged = [], [], [], []
    valid_referenced = defaultdict(list)
    valid_orphans = []
    counts = defaultdict(int)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for index, (provider, path, valid, error) in enumerate(executor.map(validate_entry, entries), 1):
            counts[provider] += 1
            if index % 250 == 0:
                print(f"[PDF 무결성] 진행: {index}/{len(entries)}", flush=True)
            relative = path.relative_to(ROOT).as_posix()
            linked = references.get(path_key(path), [])
            item = {"provider": provider, "original_path": relative, "valid": valid,
                    "reason": error, "bytes": path.stat().st_size,
                    "article_numbers": [str(r["article_number"]) for r in linked]}
            if not linked:
                orphans.append(item)
                if valid:
                    valid_orphans.append((path, item))
            if not valid:
                if provider == "designcon":
                    item["archived_incomplete"] = "incomplete" in {part.casefold() for part in path.parts}
                    manual.append(item)
                elif linked:
                    for record in linked:
                        if record["provider"] == provider:
                            damaged.append({**record, **item, "article_number": str(record["article_number"])})
                else:
                    unmanaged.append(item)
            elif linked:
                valid_referenced[(provider, path.stat().st_size)].append(path)

    # Hash only size-matched orphan/reference candidates.
    hash_cache, duplicates = {}, []
    for path, item in valid_orphans:
        candidates = valid_referenced.get((item["provider"], item["bytes"]), [])
        if not candidates:
            continue
        digest = sha256(path)
        for other in candidates:
            other_key = path_key(other)
            if other_key not in hash_cache:
                hash_cache[other_key] = sha256(other)
            if hash_cache[other_key] == digest:
                duplicates.append({**item, "sha256": digest,
                                   "retained_path": other.relative_to(ROOT).as_posix()})
                break
    manual.extend({**item, "manual_restore_required": True} for item in missing
                  if item["provider"] == "designcon")
    return {"checked_files": len(entries), "by_provider": dict(counts),
            "damaged": damaged, "missing": missing, "orphans": orphans,
            "duplicates": duplicates, "manual_review": manual, "unmapped_damaged": unmanaged}


def apply_repairs(connection, report, stamp):
    repairs = [r for r in report["damaged"] + report["missing"] if r["provider"] in AUTO_PROVIDERS]
    unique = {(r["provider"], str(r["article_number"])): r for r in repairs}
    if not unique:
        return 0
    # Record intent before mutation so interruption cannot lose the repair queue.
    queue_repairs([{**r, "detected_at": utcnow().isoformat()} for r in unique.values()])
    moves = []
    try:
        with connection.cursor() as cursor:
            moved_paths = set()
            for item in report["damaged"]:
                if item["provider"] not in AUTO_PROVIDERS:
                    continue
                source = safe_database_path(item["original_path"])
                if source is None or source in moved_paths:
                    continue
                destination = QUARANTINE_DIR / stamp / item["original_path"]
                destination.resolve().relative_to(QUARANTINE_DIR.resolve())
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                moves.append((source, destination))
                moved_paths.add(source)
                item["quarantine_path"] = destination.relative_to(ROOT).as_posix()
            for item in unique.values():
                cursor.execute("UPDATE papers SET pdf_available=0,pdf_local_path=NULL "
                               "WHERE article_number=%s AND source_system=%s",
                               (item["article_number"], item["provider"]))
        connection.commit()
    except BaseException:
        connection.rollback()
        for source, destination in reversed(moves):
            if destination.exists() and not source.exists():
                shutil.move(str(destination), str(source))
        raise
    return len(unique)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workers", type=int, choices=range(1, 9), default=4)
    args = parser.parse_args()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = STATE_DIR / "routine.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        print("[OUTCOME] SKIPPED_LOCKED: PDF acquisition or inspection is active.")
        return 75
    os.close(descriptor)
    connection = None
    try:
        connection = pymysql.connect(**DB)
        report = inspect_files(fetch_records(connection), args.workers)
        report.update(started_at=stamp, finished_at=utcnow().isoformat(), dry_run=args.dry_run)
        report["repairs_queued"] = 0 if args.dry_run else apply_repairs(connection, report, stamp)
        for name in ("damaged", "missing", "orphans", "duplicates", "manual_review", "unmapped_damaged"):
            report[name + "_count"] = len(report[name])
        actionable_manual = [r for r in report["manual_review"] if not r.get("archived_incomplete")]
        pending_repairs = args.dry_run and (report["damaged"] or report["missing"])
        report["status"] = "needs_attention" if (
            actionable_manual or report["unmapped_damaged"] or pending_repairs
        ) else "completed"
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        report_path = LOG_DIR / f"pdf_integrity_{stamp}.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items() if not isinstance(v, list)}, ensure_ascii=False))
        print(f"[보고서] {report_path}")
        return 1 if report["status"] == "needs_attention" else 0
    finally:
        if connection is not None:
            connection.close()
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
