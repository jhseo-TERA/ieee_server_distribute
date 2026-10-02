#!/usr/bin/env python3
"""Build a reviewable archive repair manifest from cached publisher evidence.

This tool never connects to a database, changes PDFs, or performs writes outside
the selected output directory. Crossref evidence must resolve to the exact IEEE
document identifier; title similarity alone never establishes identity.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

ROOT = Path(__file__).resolve().parents[1]
EDITABLE_FIELDS = ("title", "authors", "year", "source_name", "source_type", "doi")


def unescape_text(value: object) -> str:
    text = str(value or "")
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return unicodedata.normalize("NFKC", text)


def clean_text(value: object) -> str:
    text = unescape_text(value)
    return " ".join(re.sub(r"</?[A-Za-z][A-Za-z0-9:_-]*(?:\s[^<>]*)?>", "", text).split())


def normalize_text(value: object) -> str:
    return re.sub(r"[^\w]", "", clean_text(value).casefold())


def clean_title(value: object) -> str:
    text = unescape_text(value)
    text = re.sub(r"<sup>([^<]+)</sup>", r"^\1", text, flags=re.I)
    text = clean_text(text)
    # Crossref sometimes returns simple TeX typography in an otherwise plain
    # title. Keep the scientific characters while removing those wrappers.
    text = re.sub(r"\\(?:mathrm|text|hbox)\s*\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\mu\b", "μ", text)
    text = re.sub(r"\\times\b", "×", text)
    text = text.replace("\\ ", " ").replace("\\,", " ")
    if "$" in text and "\\" not in text:
        text = text.replace("$", "").replace("_", "").replace("{", "").replace("}", "")
    text = re.sub(r"(?<=\d)\^(st|nd|rd|th)\b", r"\1", text)
    return " ".join(text.split())


def document_urls(item: dict) -> list[str]:
    primary = (item.get("resource") or {}).get("primary") or {}
    return [primary.get("URL", ""), item.get("URL", "")] + [
        link.get("URL", "") for link in item.get("link") or []
    ]


def matches_ieee_document(item: dict, article_number: str) -> bool:
    """Do not accept a title match, numeric DOI suffix, or non-IEEE host."""
    expected = str(article_number)
    if not expected.isdecimal():
        return False
    for url in document_urls(item):
        parsed = urlparse(url)
        if parsed.hostname not in ("ieeexplore.ieee.org", "www.ieeexplore.ieee.org"):
            continue
        match = re.fullmatch(r"/document/(\d+)/?", parsed.path)
        if match and match.group(1) == expected:
            return True
        if parse_qs(parsed.query).get("arnumber") == [expected]:
            return True
    return False


def registered_source(item: dict, sources: list[dict]) -> dict | None:
    """Classify only into an existing source with direct venue/ISSN evidence."""
    issns = set(item.get("ISSN") or [])
    venues = [clean_text(v) for v in item.get("container-title") or []]
    matches = []
    for source in sources:
        if source.get("system") != "ieee":
            continue
        configured_issns = {source.get("issn"), *(source.get("issn_aliases") or [])} - {None, ""}
        if configured_issns & issns:
            matches.append(source)
        elif source.get("type") == "conference" and source.get("title_pattern"):
            if any(re.search(source["title_pattern"], venue, re.I) for venue in venues):
                matches.append(source)
    unique = {source["name"]: source for source in matches}
    return next(iter(unique.values())) if len(unique) == 1 else None


def publication_year(item: dict) -> str | None:
    for field in ("published", "published-print", "published-online", "issued"):
        parts = (item.get(field) or {}).get("date-parts") or []
        try:
            year = int(parts[0][0])
        except (IndexError, TypeError, ValueError):
            continue
        if 1800 <= year <= 2100:
            return str(year)
    return None


def metadata_from_item(item: dict, old: dict, sources: list[dict]) -> dict:
    result = {field: old.get(field) for field in EDITABLE_FIELDS}
    title = clean_title((item.get("title") or [""])[0])
    if title:
        result["title"] = title
    authors = []
    for author in item.get("author") or []:
        name = clean_text(author.get("name") or " ".join(
            part for part in (author.get("given"), author.get("family")) if part
        ))
        if name:
            authors.append(name)
    if authors:
        result["authors"] = "; ".join(authors)
    if year := publication_year(item):
        result["year"] = year
    doi = clean_text(item.get("DOI")).lower()
    if re.fullmatch(r"10\.(?:1109|23919)/\S+", doi):
        result["doi"] = doi
    if source := registered_source(item, sources):
        result["source_name"] = source["name"]
        result["source_type"] = source["type"]
    return result


def build_manifest(evidence_dir: Path, config: Path, pdf_dir: Path) -> dict:
    read = lambda name: json.loads((evidence_dir / name).read_text(encoding="utf-8"))
    rows = read("current_rows.json")
    pdfs = read("pdf_first_pages.json")
    sources = json.loads(config.read_text(encoding="utf-8"))["sources"]
    doi_records = read("crossref_dois.json")
    search_records = read("crossref_title_search.json")
    fallback_path = evidence_dir / "pdf_verified_fallbacks.json"
    fallbacks = read("pdf_verified_fallbacks.json") if fallback_path.exists() else {}
    result = []
    for old in rows:
        key = old["article_number"]
        pdf = pdfs.get(key, {})
        pdf_path = pdf_dir / f"{key}.pdf"
        evidence = {
            "document_url": f"https://ieeexplore.ieee.org/document/{key}/",
            "pdf_file": str(pdf_path.resolve()),
            "pdf_sha256": hashlib.sha256(pdf_path.read_bytes()).hexdigest()
            if pdf_path.exists() else None,
        }
        candidates = [entry.get("item") for entry in doi_records.values()]
        candidates += search_records.get(key, {}).get("items", [])
        candidates = {
            str(item.get("DOI", "")).lower(): item
            for item in candidates if item
            and item.get("type") in ("journal-article", "proceedings-article")
            and matches_ieee_document(item, key)
        }
        row = {"id": old["id"], "article_number": key, "old": old,
               "evidence": evidence, "status": "quarantine", "changes": {}}
        if "Document Removed From IEEE Xplore" in pdf.get("text", ""):
            row["reason"] = "publisher_removed_notice_replaces_article_pdf"
        elif not candidates and key in fallbacks:
            fallback = fallbacks[key]
            embedded = pdf.get("metadata") or {}
            title = clean_title(embedded.get("/Title"))
            body = normalize_text(pdf.get("text", ""))
            authors = [author.strip() for author in fallback.get("authors", "").split(";") if author.strip()]
            exact = embedded.get("/IEEE Article ID") == key and normalize_text(title) in body
            exact = exact and bool(authors) and all(normalize_text(author) in body for author in authors)
            if not exact:
                row["reason"] = "pdf_fallback_identity_or_text_mismatch"
            else:
                new = {field: old.get(field) for field in EDITABLE_FIELDS}
                new.update(title=title, authors="; ".join(authors))
                row["new"] = new
                row["changes"] = {field: {"old": old.get(field), "new": new[field]}
                                  for field in ("title", "authors") if old.get(field) != new[field]}
                row["status"] = "ready" if row["changes"] else "verified_unchanged"
                row["confidence"] = "embedded_ieee_article_id_and_pdf_first_page"
                row["doi_status"] = "unconfirmed_keep_blank"
                evidence.update({"pdf_title_exact_after_normalization": True,
                                 "embedded_ieee_article_id": key,
                                 "pdf_review": fallback["review"]})
        elif len(candidates) != 1:
            row["reason"] = "no_exact_document_registration" if not candidates else "multiple_doi_registrations"
            row["candidate_dois"] = sorted(candidates)
        else:
            doi, item = next(iter(candidates.items()))
            new = metadata_from_item(item, old, sources)
            if not new.get("title") or not re.fullmatch(r"10\.(?:1109|23919)/\S+", doi):
                row["reason"] = "incomplete_or_unexpected_registration"
            else:
                row["new"] = new
                row["changes"] = {
                    field: {"old": old.get(field), "new": new[field]}
                    for field in EDITABLE_FIELDS
                    if str(old.get(field) or "") != str(new.get(field) or "")
                }
                row["status"] = "ready" if row["changes"] else "verified_unchanged"
                row["confidence"] = "exact_ieee_document_url"
                evidence.update({
                    "crossref_api_url": "https://api.crossref.org/works/" + quote(doi, safe=""),
                    "crossref_document_urls": document_urls(item),
                    "registered_title": new["title"],
                    "registered_venue": item.get("container-title"),
                    "registered_doi": doi,
                    "pdf_title_exact_after_normalization": bool(normalize_text(new["title"]))
                    and normalize_text(new["title"]) in normalize_text(pdf.get("text", "")),
                })
        result.append(row)
    summary = {
        "target_rows": len(result),
        "status_counts": dict(Counter(row["status"] for row in result)),
        "changed_fields": dict(Counter(field for row in result for field in row["changes"])),
        "doi_filled": sum(not row["old"].get("doi") and "doi" in row["changes"] for row in result),
        "archive_classified": sum(row["old"]["source_name"] == "IEEE Archive"
                                  and "source_name" in row["changes"] for row in result),
        "quarantine": [{"article_number": row["article_number"], "reason": row["reason"]}
                       for row in result if row["status"] == "quarantine"],
    }
    return {"created_at": datetime.now(timezone.utc).isoformat(), "schema_version": 1,
            "mode": "proposals_only_no_database_writes", "summary": summary, "rows": result}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "config/metadata_sources.json")
    parser.add_argument("--pdf-dir", type=Path, default=ROOT / "ieee-pdf")
    args = parser.parse_args()
    manifest = build_manifest(args.evidence_dir, args.config, args.pdf_dir)
    output = args.evidence_dir / "verified_proposals.json"
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
