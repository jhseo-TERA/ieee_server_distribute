#!/usr/bin/env python3
"""Link a validated Drive PDF recovery bundle to MySQL without deleting rows.

Dry-run is the default.  ``--apply`` creates canonical JLT aliases when needed,
then performs one database transaction that:

* marks every validated flat-provider PDF available and favorite;
* inserts missing numeric IEEE archive rows from embedded PDF metadata; and
* upserts content-deduplicated DesignCon records, including valid Source files.

No SQL DELETE statement or filesystem deletion is present in this script.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path, PurePosixPath

import pymysql
from dotenv import load_dotenv
from pypdf import PdfReader


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.import_designcon_to_db import (  # noqa: E402
    document_kind,
    issue_from_path,
    title_from_filename,
)
from scripts.migrate_jlt_crossref_keys import (  # noqa: E402
    build_maps,
    jlt_doi_key,
    latest_jlt_workbook,
)
from scripts.restore_drive_zip_bundle import (  # noqa: E402
    copy_without_overwrite,
    fs_path,
    path_exists,
    sha256_file,
)


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

DOI_RE = re.compile(r"10\.1109/[A-Za-z0-9._()/:+-]+", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
DESIGNCON_YEAR_RE = re.compile(r"^DesignCon(?P<year>20\d{2})$")


def clean_text(value: object, limit: int | None = None) -> str | None:
    text = unicodedata.normalize("NFKC", html.unescape(str(value or "")))
    text = TAG_RE.sub("", text).replace("\x00", " ")
    text = WHITESPACE_RE.sub(" ", text).strip()
    if not text:
        return None
    return text[:limit] if limit is not None else text


def clean_doi(value: object) -> str | None:
    match = DOI_RE.search(str(value or ""))
    if not match:
        return None
    return match.group(0).rstrip(".,;:)]}").lower()


def good_title(value: str | None, article_number: str) -> bool:
    if not value or len(value) < 5:
        return False
    lowered = value.casefold().strip()
    generic = {
        "untitled",
        "document",
        "full text",
        "main document",
        "none",
        article_number.casefold(),
    }
    if lowered in generic or lowered.endswith((".doc", ".docx", ".pdf")):
        return False
    return True


def source_name_from_venue(venue: str | None) -> str:
    value = clean_text(venue) or "IEEE Archive"
    normalized = value.casefold()
    known = (
        ("open journal of the solid-state circuits society", "OJSSC"),
        ("journal of solid-state circuits", "JSSC"),
        ("solid-state circuits letters", "SSCL"),
        ("solid-state circuits magazine", "SSC-M"),
        ("photonics technology letters", "PTL"),
        ("microwave and wireless technology letters", "MWTL"),
        ("journal of lightwave technology", "JLT"),
        ("international solid-state circuits conference", "ISSCC"),
        ("custom integrated circuits conference", "CICC"),
        ("bipolar/bicmos circuits and technology meeting", "BCTM"),
        ("bicmos and compound semiconductor", "BCICTS"),
        ("circuits and systems ii", "TCAS-II"),
        ("circuits and systems i:", "TCAS-I"),
        ("microwave theory and techniqu", "TMTT"),
        ("microwave and wireless components letters", "MWCL"),
        ("vlsi technology", "VLSI-Tech"),
        ("vlsi circuits", "VLSI-Circuits"),
        ("asian solid-state circuits", "ASSCC"),
        ("european solid-state circuits", "ESSCIRC"),
        ("proceedings of esscirc", "ESSCIRC"),
        ("esscirc conference", "ESSCIRC"),
        ("radio frequency integrated circuits", "RFIC"),
        ("international symposium on circuits and", "ISCAS"),
        ("midwest symposium", "MWSCAS"),
        ("asia pacific conference on", "APCCAS"),
    )
    for fragment, code in known:
        if fragment in normalized:
            return code
    parenthetical = re.findall(r"\(([A-Z][A-Z0-9-]{2,12})\)", value)
    if parenthetical:
        value = parenthetical[-1]
    return {
        "A-SSCC": "ASSCC",
        "CIRC": "ESSCIRC",
        "VLSIC": "VLSI-Circuits",
        "ISCAS2013": "ISCAS",
    }.get(value, value[:50])


DOI_VENUE_CODES = {
    "asscc": "ASSCC",
    "esscirc": "ESSCIRC",
    "isscc": "ISSCC",
    "jssc": "JSSC",
    "rfic": "RFIC",
    "tcsi": "TCAS-I",
    "tcsii": "TCAS-II",
    "vlsic": "VLSI-Circuits",
    "vlsit": "VLSI-Tech",
}

# Some recovered IEEE PDFs expose only a session heading in /Subject and omit
# the DOI.  These document identifiers are stable IEEE Xplore identifiers, so
# keep the small exception list explicit and reviewable.
RECOVERED_DOCUMENT_VENUES = {
    "4266464": "RFIC",
    "4430353": "ESSCIRC",
    "4430354": "ESSCIRC",
    "4585951": "VLSI-Circuits",
    "4585995": "VLSI-Circuits",
    "4586004": "VLSI-Circuits",
    "4681821": "ESSCIRC",
    "7969067": "RFIC",
    "8008477": "VLSI-Circuits",
    "8008523": "VLSI-Circuits",
    "8008524": "VLSI-Circuits",
    "8008525": "VLSI-Circuits",
    "8008527": "VLSI-Circuits",
    "8008546": "VLSI-Circuits",
    "8008549": "VLSI-Circuits",
}

CONFERENCE_SOURCE_NAMES = {
    "APCCAS", "ASSCC", "BCICTS", "BCTM", "CICC", "ESSCIRC", "ESSERC",
    "ICTA", "ISCAS", "ISSCC", "MWSCAS", "NEWCAS", "OFC", "RFIC",
    "VLSI-Circuits", "VLSI-Tech",
}


def source_name_from_metadata(
    venue: str | None,
    doi: str | None = None,
    article_number: str | None = None,
) -> str:
    """Prefer stable document/DOI evidence over noisy embedded PDF subjects."""
    document_venue = RECOVERED_DOCUMENT_VENUES.get(str(article_number or ""))
    if document_venue:
        return document_venue
    match = re.match(
        r"10\.(?:1109|23919)/([a-z]+)(?:\d*\.)", str(doi or "").casefold()
    )
    if match and match.group(1) in DOI_VENUE_CODES:
        return DOI_VENUE_CODES[match.group(1)]
    return source_name_from_venue(venue)


def source_type_from_metadata(source_name: str, venue: str | None = None) -> str:
    if source_name in CONFERENCE_SOURCE_NAMES:
        return "conference"
    return source_type_from_venue(venue)


def source_type_from_venue(venue: str | None) -> str:
    value = (venue or "").casefold()
    if any(word in value for word in ("conference", "symposium", "workshop", "meeting")):
        return "conference"
    return "journal" if venue else "archive"


def parse_ieee_pdf(relative: PurePosixPath) -> tuple[dict, str]:
    article_number = relative.stem
    path = ROOT / Path(*relative.parts)
    with open(fs_path(path), "rb") as handle:
        reader = PdfReader(handle, strict=False)
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise ValueError(f"encrypted IEEE PDF cannot be opened: {relative}")
        metadata = reader.metadata or {}
        raw_title = clean_text(getattr(metadata, "title", None))
        if raw_title:
            raw_title = re.sub(
                r"(?i)^Microsoft Word\s*-\s*", "", raw_title
            ).strip()
        subject = clean_text(getattr(metadata, "subject", None))
        author = clean_text(getattr(metadata, "author", None))
        try:
            creation_date = getattr(metadata, "creation_date", None)
        except (TypeError, ValueError):
            creation_date = None
        first_page_text = ""
        if not good_title(raw_title, article_number) or not clean_doi(subject):
            try:
                first_page_text = reader.pages[0].extract_text() or ""
            except Exception:
                first_page_text = ""

    subject_parts = [part.strip() for part in (subject or "").split(";")]
    venue = clean_text(subject_parts[0]) if subject_parts else None
    subject_year = None
    if len(subject_parts) > 1:
        match = YEAR_RE.search(subject_parts[1])
        subject_year = match.group(1) if match else None
    volume = clean_text(subject_parts[2], 40) if len(subject_parts) > 2 else None
    number = clean_text(subject_parts[3], 40) if len(subject_parts) > 3 else None
    doi = clean_doi(subject) or clean_doi(first_page_text)

    creation_year = None
    if creation_date is not None:
        try:
            candidate = int(creation_date.year)
            if 1900 <= candidate <= 2100:
                creation_year = str(candidate)
        except (AttributeError, TypeError, ValueError):
            pass
    first_page_year = None
    if first_page_text:
        match = YEAR_RE.search(first_page_text[:2500])
        first_page_year = match.group(1) if match else None
    year = subject_year or first_page_year or creation_year

    if good_title(raw_title, article_number):
        title = raw_title
        quality = "embedded-title"
        if subject_year and venue:
            quality = "embedded-core"
    else:
        title = f"Recovered IEEE document {article_number}"
        quality = "metadata-review-required"

    issue_parts = []
    if volume:
        issue_parts.append(f"Vol. {volume}")
    if number:
        issue_parts.append(f"No. {number}")
    issue = ", ".join(issue_parts)[:120] or None
    source_name = source_name_from_metadata(venue, doi, article_number)
    row = {
        "article_number": article_number,
        "title": title,
        "authors": author,
        "year": year,
        "source_name": source_name,
        "source_type": source_type_from_metadata(source_name, venue),
        "source_system": "ieee",
        "issue": issue,
        "url": f"https://ieeexplore.ieee.org/document/{article_number}/",
        "pdf_local_path": relative.as_posix(),
        "pdf_available": 1,
        "is_favorite": 1,
        "doi": doi,
    }
    return row, quality


def latest_valid_report() -> tuple[Path, dict]:
    candidates = sorted((ROOT / "drive-zip-extracted").glob("PDF_VALIDATION_*.json"))
    for path in reversed(candidates):
        report = json.loads(path.read_text(encoding="utf-8"))
        if report.get("invalid_count") == 0 and report.get("valid_count"):
            return path, report
    raise RuntimeError("no all-valid PDF validation report found")


def build_designcon_rows(paths: list[PurePosixPath]) -> tuple[list[dict], int]:
    candidates: dict[tuple[str, str], dict] = {}
    scanned = 0
    for relative in sorted(paths, key=lambda item: item.as_posix().casefold()):
        if len(relative.parts) < 3:
            raise ValueError(f"unexpected DesignCon path: {relative}")
        match = DESIGNCON_YEAR_RE.match(relative.parts[1])
        if not match:
            raise ValueError(f"DesignCon year directory missing: {relative}")
        scanned += 1
        year = match.group("year")
        path = ROOT / Path(*relative.parts)
        digest = sha256_file(path)
        relative_text = relative.as_posix()
        if len(relative_text) > 400:
            raise ValueError(f"DesignCon path exceeds DB limit: {relative_text}")
        kind = document_kind(relative.name)
        record = {
            "article_number": f"designcon-{year}-{digest[:24]}",
            "title": title_from_filename(relative.name),
            "authors": None,
            "year": year,
            "source_name": "DesignCon",
            "source_type": "conference",
            "source_system": "designcon",
            "issue": issue_from_path(path, kind)[:120],
            "url": None,
            "pdf_local_path": relative_text,
            "pdf_available": 1,
            "is_favorite": 1,
            "doi": None,
        }
        key = (year, digest)
        current = candidates.get(key)
        if current is None or (
            len(relative_text), relative_text.casefold()
        ) < (
            len(current["pdf_local_path"]), current["pdf_local_path"].casefold()
        ):
            candidates[key] = record
    rows = sorted(candidates.values(), key=lambda row: row["article_number"])
    return rows, scanned - len(rows)


def connect_db():
    return pymysql.connect(**DB)


def load_existing_keys(conn) -> dict[str, tuple[str, str]]:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT article_number, source_system FROM papers "
            "WHERE article_number IS NOT NULL"
        )
        rows = cursor.fetchall()
    result: dict[str, tuple[str, str]] = {}
    for article_number, source_system in rows:
        key = str(article_number).casefold()
        if key in result:
            raise RuntimeError(f"case-insensitive duplicate DB key: {article_number}")
        result[key] = (str(article_number), str(source_system))
    return result


def build_jlt_maps():
    workbook, frame = latest_jlt_workbook()
    by_uri, _by_ieee, _by_title, _valid_keys = build_maps(frame)
    doi_by_key: dict[str, str] = {}
    for _, row in frame.iterrows():
        target = jlt_doi_key(row.get("DOI"))
        doi = clean_doi(row.get("DOI"))
        if target and doi:
            doi_by_key[target.casefold()] = doi
    return Path(workbook), by_uri, doi_by_key


def ensure_jlt_aliases(aliases: list[dict]) -> Counter:
    statuses: Counter = Counter()
    for alias in aliases:
        source = ROOT / Path(*alias["source_path"].split("/"))
        target = ROOT / Path(*alias["target_path"].split("/"))
        if path_exists(target):
            status = (
                "canonical-existing-identical"
                if sha256_file(source) == sha256_file(target)
                else "canonical-existing-preserved"
            )
        else:
            status = copy_without_overwrite(source, target)
        alias["alias_status"] = status
        statuses[status] += 1
    return statuses


FLAT_UPDATE_SQL = """
UPDATE papers SET
    pdf_available=1,
    pdf_local_path=%(pdf_local_path)s,
    is_favorite=1,
    doi=CASE
        WHEN (doi IS NULL OR doi='') AND %(doi)s IS NOT NULL THEN %(doi)s
        ELSE doi
    END
WHERE article_number=%(article_number)s
"""

INSERT_SQL = """
INSERT INTO papers (
    article_number, title, authors, year, source_name, source_type,
    source_system, issue, url, pdf_local_path, pdf_available, is_favorite, doi
) VALUES (
    %(article_number)s, %(title)s, %(authors)s, %(year)s, %(source_name)s,
    %(source_type)s, %(source_system)s, %(issue)s, %(url)s,
    %(pdf_local_path)s, %(pdf_available)s, %(is_favorite)s, %(doi)s
)
ON DUPLICATE KEY UPDATE
    pdf_available=1,
    pdf_local_path=VALUES(pdf_local_path),
    is_favorite=1,
    doi=CASE
        WHEN (papers.doi IS NULL OR papers.doi='') AND VALUES(doi) IS NOT NULL
        THEN VALUES(doi)
        ELSE papers.doi
    END
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    report_path, report = (
        (args.report.resolve(), json.loads(args.report.read_text(encoding="utf-8")))
        if args.report
        else latest_valid_report()
    )
    if report.get("invalid_count") != 0:
        raise RuntimeError("validation report contains invalid PDFs")
    valid_paths = [PurePosixPath(path) for path in report["valid_paths"]]
    if len(valid_paths) != int(report["valid_count"]):
        raise RuntimeError("validation report count mismatch")
    if len({path.as_posix().casefold() for path in valid_paths}) != len(valid_paths):
        raise RuntimeError("validation report contains duplicate paths")
    for relative in valid_paths:
        if not path_exists(ROOT / Path(*relative.parts)):
            raise FileNotFoundError(f"validated project file is missing: {relative}")

    conn = connect_db()
    try:
        existing = load_existing_keys(conn)
    finally:
        conn.close()
    workbook, jlt_by_uri, jlt_doi_by_key = build_jlt_maps()

    flat_updates: list[dict] = []
    new_ieee: list[dict] = []
    jlt_aliases: list[dict] = []
    metadata_quality: Counter = Counter()
    designcon_paths: list[PurePosixPath] = []

    for relative in valid_paths:
        root_name = relative.parts[0]
        if root_name == "DesignCon":
            designcon_paths.append(relative)
            continue
        stem = relative.stem
        exact = existing.get(stem.casefold())
        doi = None
        if root_name == "ieee-pdf":
            metadata_row, quality = parse_ieee_pdf(relative)
            metadata_quality[quality] += 1
            doi = metadata_row["doi"]
            if exact is None:
                new_ieee.append(metadata_row)
                continue
            target_key, target_system = exact
            if target_system != "ieee":
                raise RuntimeError(f"IEEE file maps to non-IEEE row: {relative}")
            flat_updates.append(
                {
                    "article_number": target_key,
                    "pdf_local_path": relative.as_posix(),
                    "doi": doi,
                }
            )
            continue

        if root_name == "nature-pdf":
            if exact is None or exact[1] != "nature":
                raise RuntimeError(f"Nature PDF has no exact DB row: {relative}")
            flat_updates.append(
                {
                    "article_number": exact[0],
                    "pdf_local_path": relative.as_posix(),
                    "doi": None,
                }
            )
            continue

        if root_name == "optica-pdf":
            if exact is not None:
                if exact[1] != "optica":
                    raise RuntimeError(f"Optica PDF maps to another source: {relative}")
                flat_updates.append(
                    {
                        "article_number": exact[0],
                        "pdf_local_path": relative.as_posix(),
                        "doi": jlt_doi_by_key.get(exact[0].casefold()),
                    }
                )
                continue
            target = jlt_by_uri.get(stem.casefold())
            if target is None:
                raise RuntimeError(f"unmapped legacy Optica/JLT PDF: {relative}")
            target_row = existing.get(target.casefold())
            if target_row is None or target_row[1] != "optica":
                raise RuntimeError(f"canonical JLT DB row missing: {target}")
            canonical_path = f"optica-pdf/{target_row[0]}.pdf"
            jlt_aliases.append(
                {
                    "source_path": relative.as_posix(),
                    "target_path": canonical_path,
                    "target_key": target_row[0],
                }
            )
            flat_updates.append(
                {
                    "article_number": target_row[0],
                    "pdf_local_path": canonical_path,
                    "doi": jlt_doi_by_key.get(target.casefold()),
                }
            )
            continue
        raise RuntimeError(f"unexpected validated root: {relative}")

    designcon_rows, designcon_duplicates = build_designcon_rows(designcon_paths)
    raw_flat_update_count = len(flat_updates)
    flat_by_key: dict[str, dict] = {}
    for update in flat_updates:
        key = update["article_number"].casefold()
        current = flat_by_key.get(key)
        if current is None:
            flat_by_key[key] = update
            continue
        if current["pdf_local_path"] != update["pdf_local_path"]:
            raise RuntimeError(
                f"conflicting PDF paths for DB key {update['article_number']}: "
                f"{current['pdf_local_path']} and {update['pdf_local_path']}"
            )
        if current.get("doi") is None and update.get("doi") is not None:
            current["doi"] = update["doi"]
    flat_updates = sorted(flat_by_key.values(), key=lambda row: row["article_number"].casefold())
    target_keys = [row["article_number"].casefold() for row in flat_updates]
    target_keys.extend(row["article_number"].casefold() for row in new_ieee)
    target_keys.extend(row["article_number"].casefold() for row in designcon_rows)
    if len(target_keys) != len(set(target_keys)):
        raise RuntimeError("multiple recovered papers resolve to the same DB key")

    summary = {
        "validation_report": str(report_path),
        "validated_physical_pdfs": len(valid_paths),
        "flat_physical_pdfs": len(valid_paths) - len(designcon_paths),
        "flat_existing_updates": len(flat_updates),
        "flat_duplicate_mappings": raw_flat_update_count - len(flat_updates),
        "new_ieee_rows": len(new_ieee),
        "ieee_metadata_quality": dict(sorted(metadata_quality.items())),
        "legacy_jlt_aliases": len(jlt_aliases),
        "jlt_workbook": str(workbook),
        "designcon_physical_pdfs": len(designcon_paths),
        "designcon_content_duplicates": designcon_duplicates,
        "designcon_unique_rows": len(designcon_rows),
        "unique_pdf_backed_rows": len(flat_updates) + len(new_ieee) + len(designcon_rows),
        "filesystem_deletes": 0,
        "sql_deletes": 0,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if not args.apply:
        print("Dry run completed; no files or database rows were changed.")
        return 0

    alias_statuses = ensure_jlt_aliases(jlt_aliases)
    conn = connect_db()
    try:
        with conn.cursor() as cursor:
            if flat_updates:
                cursor.executemany(FLAT_UPDATE_SQL, flat_updates)
            if new_ieee:
                cursor.executemany(INSERT_SQL, new_ieee)
            if designcon_rows:
                cursor.executemany(INSERT_SQL, designcon_rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    summary["jlt_alias_statuses"] = dict(alias_statuses)
    summary["applied_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    audit = {
        "summary": summary,
        "new_ieee_rows": new_ieee,
        "legacy_jlt_aliases": jlt_aliases,
        "designcon_rows": designcon_rows,
    }
    audit_path = report_path.with_name(
        "SQL_RECONCILE_" + time.strftime("%Y%m%d_%H%M%S") + ".json"
    )
    with audit_path.open("x", encoding="utf-8") as handle:
        json.dump(audit, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"Audit: {audit_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
