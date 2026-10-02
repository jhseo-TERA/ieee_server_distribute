"""Rebuild an empty SerDes survey from restored local PDFs and source workbooks.

No source files are removed. Inspection is read-only with respect to SQL.
Metadata apply preserves a before/after audit and immutable PDF abstract snapshots.
Survey bootstrap refuses nonempty survey tables, protecting subsequent reviews.
Run with --stage inspect, metadata, or survey; writes require --apply.
The enrich stage resumes only a bootstrap stopped before metric extraction.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import sys
import unicodedata

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.reconcile_recovered_pdf_bundle import clean_text, source_name_from_metadata
from scripts.restore_drive_zip_bundle import fs_path, sha256_file
from scripts.serdes_data_pipeline import (
    db_connect, screen_venue, extract_titles, extract_abstracts, import_wlink,
    store_abstract_snapshot, classify_link_media, classify_link_subtypes,
    classify_measurement_scopes, build_implementation_families,
)
from scripts.sync_user_serdes_survey import audit_and_sync, SURVEY_WORKBOOK_NAME
from serdes_fom import regenerate_fom

CANONICAL_VENUES = {
    "OJSSC", "JSSC", "SSCL", "SSC-M", "PTL", "MWTL", "JLT", "ISSCC",
    "CICC", "BCTM", "BCICTS", "TCAS-I", "TCAS-II", "TMTT", "MWCL",
    "VLSI-Tech", "VLSI-Circuits", "ASSCC", "ESSCIRC", "ESSERC", "RFIC",
    "ISCAS", "MWSCAS", "APCCAS", "ICTA", "NEWCAS",
}
CONFERENCE_VENUES = {
    "ISSCC", "CICC", "BCTM", "BCICTS", "VLSI-Tech", "VLSI-Circuits", "ASSCC",
    "ESSCIRC", "ESSERC", "RFIC", "ISCAS", "MWSCAS", "APCCAS", "ICTA", "NEWCAS",
}
ABSTRACT_START = re.compile(r"(?:^|\n)\s*Abstract\s*(?:[—–−:‐-]\s*|\n)", re.I)
ABSTRACT_END = re.compile(
    r"\b(?:Index\s+Terms|Keywords|Key\s+words)\s*[—–−:‐-]?|"
    r"(?:^|\n)\s*(?:I\.|1[.)]?)\s*INTRODUCTION\b", re.I,
)


def extract_marked_abstract(text: str) -> tuple[str | None, str]:
    """Accept only a bounded first-page Abstract section, never infer a summary."""
    text = unicodedata.normalize("NFKC", text).replace("\r", "")
    start = ABSTRACT_START.search(text)
    if not start:
        return None, "no_explicit_abstract"
    tail = text[start.end():]
    end = ABSTRACT_END.search(tail)
    if not end:
        return None, "no_explicit_end"
    abstract = tail[:end.start()]
    abstract = re.sub(r"(?<=[a-z])-\s*\n\s*(?=[a-z])", "", abstract)
    abstract = re.sub(r"\s+", " ", abstract).strip()
    if not 160 <= len(abstract) <= 5000 or len(abstract.split()) < 30:
        return None, "implausible_abstract_length"
    if "\ufffd" in abstract or "Authorized licensed use" in abstract:
        return None, "text_quality_review"
    return abstract, "explicit_abstract_to_heading"


def canonical_venue(paper: dict, subject: str) -> str:
    original = paper["source_name"]
    for value in (subject.split(";")[0], original):
        canonical = source_name_from_metadata(
            value, paper.get("doi"), paper.get("article_number")
        )
        if canonical in CANONICAL_VENUES:
            return canonical
    return original


def collect_pdf_metadata(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, article_number, title, source_name, source_type, doi, "
                    "pdf_local_path FROM papers WHERE source_system='ieee' "
                    "AND pdf_available=1 ORDER BY id")
        papers = cur.fetchall()
    results = []
    for index, paper in enumerate(papers, 1):
        item = {"paper": paper}
        try:
            path = (ROOT / paper["pdf_local_path"]).resolve()
            if not path.is_relative_to(ROOT):
                raise ValueError("PDF path escapes workspace")
            with open(fs_path(path), "rb") as handle:
                reader = PdfReader(handle, strict=False)
                if reader.is_encrypted and reader.decrypt("") == 0:
                    raise ValueError("PDF requires password")
                subject = clean_text((reader.metadata or {}).get("/Subject")) or ""
                first_page = reader.pages[0].extract_text() or ""
            abstract, reason = extract_marked_abstract(first_page)
            item.update(subject=subject, first_page_text=first_page,
                        source_name=canonical_venue(paper, subject),
                        abstract=abstract, abstract_status=reason,
                        pdf_sha256=sha256_file(path))
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"
        results.append(item)
        if index % 100 == 0:
            print(f"PDF inspection {index}/{len(papers)}", flush=True)
    return results


def apply_metadata(conn, items: list[dict]) -> dict:
    stats = Counter()
    with conn.cursor() as cur:
        for item in items:
            if item.get("error"):
                stats["skipped_error"] += 1
                continue
            paper = item["paper"]
            venue = item["source_name"]
            source_type = ("conference" if venue in CONFERENCE_VENUES else "journal") if venue in CANONICAL_VENUES else paper["source_type"]
            if venue != paper["source_name"] or source_type != paper["source_type"]:
                cur.execute("UPDATE papers SET source_name=%s, source_type=%s "
                            "WHERE id=%s AND source_name=%s AND source_type <=> %s",
                            (venue, source_type, paper["id"], paper["source_name"], paper["source_type"]))
                stats["venue_updates"] += cur.rowcount
            if item.get("abstract"):
                abstract_id, created = store_abstract_snapshot(cur, paper, {
                    "abstract": item["abstract"],
                    "abstract_url": f"/pdf/{paper['article_number']}#page=1",
                    "provider_record_id": f"{paper['article_number']}:{item['pdf_sha256']}",
                }, provider="local_pdf")
                item["abstract_id"] = abstract_id
                stats["abstract_snapshots"] += int(created)
        conn.commit()
    return dict(stats)


def bootstrap_survey(conn, folder: Path, output: Path) -> dict:
    with conn.cursor() as cur:
        for table in ("serdes_implementations", "serdes_paper_screenings",
                      "serdes_measurements", "serdes_screening_runs"):
            cur.execute(f"SELECT COUNT(*) n FROM {table}")
            if cur.fetchone()["n"]:
                raise RuntimeError(f"Refusing bootstrap: {table} is not empty; use targeted pipeline steps")
        cur.execute("SELECT DISTINCT source_name FROM papers WHERE source_system='ieee' ORDER BY source_name")
        venues = [row["source_name"] for row in cur.fetchall()]
    report = {"screenings": []}
    for venue in venues:
        result = screen_venue(conn, venue, "all")
        report["screenings"].append(result)
        print(f"Screened {venue}: {result['targeted']} (core {result['core']}, adjacent {result['adjacent']})", flush=True)
    # Exact normalized titles only for the external reference. Unmatched rows
    # remain unlinked references and do not create fictional paper identities.
    report["wlink"] = import_wlink(conn, folder / "WLink_survey_2025.xlsx", threshold=1.0)
    print("WLink:", report["wlink"], flush=True)
    write_json(output / "bootstrap_scope.json", report)
    report.update(enrich_survey(conn, folder, output))
    return report


def enrich_survey(conn, folder: Path, output: Path) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) n FROM serdes_measurements WHERE source_kind<>'reference_xlsx'")
        if cur.fetchone()["n"]:
            raise RuntimeError("Metric extraction already started; use targeted pipeline steps")
        cur.execute("SELECT COUNT(*) n FROM serdes_screening_runs WHERE status='complete'")
        if not cur.fetchone()["n"]:
            raise RuntimeError("Complete corpus screening is required before enrichment")
    report = {}
    report["titles"] = extract_titles(conn, screened_only=True)
    report["abstracts"] = extract_abstracts(conn, screened_only=True)
    user_report = audit_and_sync(conn, folder / SURVEY_WORKBOOK_NAME, apply=True)
    write_json(output / "user_sheet_audit.json", user_report)
    report["user_sheet"] = user_report["summary"]
    print("User sheet:", report["user_sheet"], flush=True)
    report["fom"] = regenerate_fom(conn, apply=True)["summary"]
    report["media"] = classify_link_media(conn)
    report["scopes"] = classify_measurement_scopes(conn)
    report["subtypes"] = classify_link_subtypes(conn)
    report["families"] = build_implementation_families(conn)
    return report


def write_json(path: Path, content: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Reports are immutable, including on rerun.
    with path.open("x", encoding="utf-8") as handle:
        json.dump(content, handle, ensure_ascii=False, indent=2, default=str)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("inspect", "metadata", "survey", "enrich"), default="inspect")
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--source-dir", type=Path, default=ROOT / "tmp/serdes-recovery")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.stage != "inspect" and not args.apply:
        parser.error("SQL writes require --apply")
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = ROOT / "outputs/serdes_recovery" / f"{stamp}_{args.stage}"
    output.mkdir(parents=True, exist_ok=False)
    conn = db_connect()
    try:
        if args.stage in {"survey", "enrich"}:
            for filename in ("WLink_survey_2025.xlsx", SURVEY_WORKBOOK_NAME):
                if not (args.source_dir / filename).is_file():
                    raise FileNotFoundError(filename)
            runner = bootstrap_survey if args.stage == "survey" else enrich_survey
            report = runner(conn, args.source_dir, output)
        else:
            items = json.loads(args.cache.read_text(encoding="utf-8"))["items"] if args.cache else collect_pdf_metadata(conn)
            report = {"items": items, "summary": {
                "pdfs": len(items), "errors": sum(bool(i.get("error")) for i in items),
                "abstract_status": dict(Counter(i.get("abstract_status") for i in items)),
                "venue_changes": sum(i.get("source_name", i["paper"]["source_name"]) != i["paper"]["source_name"] for i in items),
            }}
            # Save the original database metadata before any mutation.
            write_json(output / "inspection.json", report)
            if args.apply:
                report["applied"] = apply_metadata(conn, items)
        write_json(output / "result.json", report)
        print(json.dumps({"output": str(output), **{k: v for k, v in report.items() if k not in {"items", "screenings"}}}, ensure_ascii=False, default=str), flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
