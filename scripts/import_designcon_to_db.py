# -*- coding: utf-8 -*-
r"""Import the organized DesignCon PDF collection into the papers table.

The importer is intentionally filename-based.  Many DesignCon files have
generic or stale PDF metadata, while their organized filenames and directory
names consistently carry the useful title, track, and document-kind fields.

Usage:
  .venv\Scripts\python.exe scripts\import_designcon_to_db.py --dry-run
  .venv\Scripts\python.exe scripts\import_designcon_to_db.py
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from collections import Counter
from pathlib import Path

import pymysql
from dotenv import load_dotenv

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
DESIGNCON_DIR = ROOT / "DesignCon"
YEAR_DIR_RE = re.compile(r"^DesignCon(?P<year>20\d{2})$")
TRAILING_DOWNLOAD_IDS_RE = re.compile(r"(?:[_ -]\d{2,}){2,}$")
CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

load_dotenv(ROOT / ".env")

DB = dict(
    host=os.getenv("DB_HOST", "127.0.0.1"),
    port=int(os.getenv("DB_PORT", "3306")),
    user=os.getenv("DB_USER", "root"),
    password=os.getenv("DB_PASSWORD", ""),
    database=os.getenv("DB_NAME", "ieee_repo"),
    charset="utf8mb4",
    connect_timeout=20,
    read_timeout=300,
    write_timeout=300,
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def document_kind(filename: str) -> str:
    value = filename.lower()
    if re.search(r"(^|[_ -])(slides?|sldies|slider|ppt)([_ .-]|$)", value):
        return "Slides"
    if re.search(r"(^|[_ -])(papers?|papar)([_ .-]|$)", value):
        return "Paper"
    if "keynote" in value:
        return "Keynote"
    if "panel" in value:
        return "Panel"
    return "Other"


def title_from_filename(filename: str) -> str:
    """Derive a readable title without pretending filename tokens are authors."""
    name = filename.strip()
    duplicate_suffix = re.search(r"(?i)\.pdf_", name)
    if duplicate_suffix:
        name = name[: duplicate_suffix.start()]
    else:
        name = re.sub(r"(?i)\.pdf$", "", name)

    canonical = re.match(
        r"(?i)^(?:(?:\d+[-_][A-Z]{1,3}\d+|Track \d{2}) - )?"
        r"(?:Paper|Slides|Keynote|Panel|Document) - (?P<title>.+)$",
        name,
    )
    if canonical:
        return re.sub(r"\s+", " ", canonical.group("title")).strip()

    # Remove conference/year prefixes and repeated document-kind/track prefixes.
    name = re.sub(
        r"(?i)^(?:designcon|dcon|dc)[-_ ]?(?:20)?\d{2}[-_ ]*", "", name
    )
    name = re.sub(
        r"(?i)^(?:samtec[-_ ]*)?dc(?:20)?\d{2}[-_ ]*", "", name
    )
    name = re.sub(
        r"(?i)^(?:\d+[-_][A-Z]{1,3}\d+[-_ ]*)?"
        r"(?:paper|papar|slides?|sldies|slider|ppt|pdf)[-_ ]*",
        "",
        name,
    )
    name = re.sub(r"(?i)^track[-_ ]*\d{1,2}[-_ ]*", "", name)
    name = re.sub(r"(?i)^(?:paper|papar|slides?|sldies|slider|ppt|pdf)[-_ ]*", "", name)
    name = re.sub(r"^\d{1,2}[-_ ]+", "", name)
    name = TRAILING_DOWNLOAD_IDS_RE.sub("", name)

    name = name.replace("_", " ").replace("-", " ")
    name = CAMEL_BOUNDARY_RE.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip(" ._-")
    return name or "Untitled DesignCon document"


def issue_from_path(path: Path, kind: str) -> str:
    documents_dir = next(
        (parent for parent in path.parents if parent.name == "Documents"),
        None,
    )
    category = ""
    if documents_dir is not None:
        relative_parts = path.relative_to(documents_dir).parts
        if len(relative_parts) > 1:
            category = relative_parts[0]

    if not category:
        match = re.search(r"(?i)track[-_ ]*0?(\d{1,2})", path.name)
        if match:
            category = f"Track {int(match.group(1)):02d}"

    parts = []
    if category and category.lower() != "other":
        parts.append(category)
    parts.append(kind)
    return " / ".join(parts)


def collect_records() -> tuple[list[dict], int]:
    """Return one row per unique (year, content hash)."""
    candidates: dict[tuple[str, str], dict] = {}
    scanned = 0

    for year_dir in sorted(DESIGNCON_DIR.glob("DesignCon20??")):
        match = YEAR_DIR_RE.match(year_dir.name)
        documents_dir = year_dir / "Documents"
        if not match or not documents_dir.is_dir():
            continue
        year = match.group("year")

        for pdf_path in sorted(
            (p for p in documents_dir.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"),
            key=lambda p: str(p).lower(),
        ):
            scanned += 1
            digest = sha256_file(pdf_path)
            relative_path = pdf_path.relative_to(ROOT).as_posix()
            if len(relative_path) > 400:
                raise ValueError(f"pdf_local_path exceeds VARCHAR(400): {relative_path}")

            kind = document_kind(pdf_path.name)
            record = dict(
                article_number=f"designcon-{year}-{digest[:24]}",
                title=title_from_filename(pdf_path.name),
                authors=None,
                year=year,
                source_name="DesignCon",
                source_type="conference",
                source_system="designcon",
                issue=issue_from_path(pdf_path, kind),
                url=None,
                pdf_local_path=relative_path,
                pdf_available=1,
            )
            key = (year, digest)
            current = candidates.get(key)
            if current is None or (
                len(relative_path), relative_path.lower()
            ) < (
                len(current["pdf_local_path"]), current["pdf_local_path"].lower()
            ):
                candidates[key] = record

    records = sorted(
        candidates.values(),
        key=lambda row: (
            row["year"],
            row["issue"],
            row["title"].lower(),
            row["pdf_local_path"].lower(),
        ),
    )
    return records, scanned


UPSERT_SQL = """
INSERT INTO papers (
    article_number, title, authors, year, source_name, source_type,
    source_system, issue, url, pdf_local_path, pdf_available
) VALUES (
    %(article_number)s, %(title)s, %(authors)s, %(year)s, %(source_name)s,
    %(source_type)s, %(source_system)s, %(issue)s, %(url)s,
    %(pdf_local_path)s, %(pdf_available)s
)
ON DUPLICATE KEY UPDATE
    title=VALUES(title),
    authors=VALUES(authors),
    year=VALUES(year),
    source_name=VALUES(source_name),
    source_type=VALUES(source_type),
    source_system=VALUES(source_system),
    issue=VALUES(issue),
    url=VALUES(url),
    pdf_local_path=VALUES(pdf_local_path),
    pdf_available=VALUES(pdf_available)
"""


def import_records(records: list[dict], prune: bool = True) -> int:
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMPORARY TABLE designcon_import_keys "
                "(article_number VARCHAR(80) PRIMARY KEY)"
            )
            cur.executemany(
                "INSERT INTO designcon_import_keys (article_number) VALUES (%s)",
                [(row["article_number"],) for row in records],
            )
            cur.executemany(UPSERT_SQL, records)

            deleted = 0
            if prune:
                deleted = cur.execute(
                    "DELETE p FROM papers p "
                    "LEFT JOIN designcon_import_keys k "
                    "ON k.article_number = p.article_number "
                    "WHERE p.source_system='designcon' "
                    "AND k.article_number IS NULL"
                )

            cur.execute(
                "CREATE OR REPLACE VIEW papers_designcon AS "
                "SELECT * FROM papers WHERE source_system='designcon'"
            )
        conn.commit()
        return deleted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def print_summary(records: list[dict], scanned: int) -> None:
    counts = Counter(row["year"] for row in records)
    print(f"Scanned PDFs: {scanned}")
    print(f"Unique DesignCon records: {len(records)}")
    print("Years: " + ", ".join(f"{year}={counts[year]}" for year in sorted(counts)))
    duplicate_count = scanned - len(records)
    if duplicate_count:
        print(f"Content duplicates skipped: {duplicate_count}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and summarize without connecting to MySQL.",
    )
    parser.add_argument(
        "--no-prune",
        action="store_true",
        help="Keep stale DesignCon rows that are no longer present on disk.",
    )
    args = parser.parse_args()

    if not DESIGNCON_DIR.is_dir():
        parser.error(f"DesignCon directory not found: {DESIGNCON_DIR}")

    records, scanned = collect_records()
    if not records:
        parser.error("No DesignCon PDFs found under DesignCon20??/Documents")
    print_summary(records, scanned)

    if args.dry_run:
        print("Dry run: database was not changed.")
        return 0

    deleted = import_records(records, prune=not args.no_prune)
    print(f"Database upserted: {len(records)}")
    if not args.no_prune:
        print(f"Stale DesignCon rows pruned: {deleted}")
    print("View refreshed: papers_designcon")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
