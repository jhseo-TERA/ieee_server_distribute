# -*- coding: utf-8 -*-
r"""Collect IEEE journal/conference metadata from the public Crossref API.

This collector never opens IEEE Xplore, a library proxy, or a VPN and never
downloads PDFs. Requests are sequential and start at least 1.1 seconds apart
(less than 1 request/second), leaving headroom for other users sharing the
public IP. HTTP 429 responses honor Retry-After and use exponential backoff.

The generated Excel files are compatible with ``import_excel_to_db.py``.

Examples:
  .venv\Scripts\python.exe scripts\fetch_ieee_crossref.py
  .venv\Scripts\python.exe scripts\fetch_ieee_crossref.py --full
  .venv\Scripts\python.exe scripts\fetch_ieee_crossref.py --sources MWCL,MWTL
  .venv\Scripts\python.exe scripts\fetch_ieee_crossref.py --sources ICTA --full
"""
import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
import os
import re
import sys
import time

import pandas as pd
import requests
from dotenv import load_dotenv

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
META_DIR = os.path.join(ROOT, "py_01_data", "00_metadata")
load_dotenv(os.path.join(ROOT, ".env"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from scripts.metadata_sources import sources, source as get_source, publication_range, select_sources
from scripts.crossref_pagination import PageAudit

API_BASE = "https://api.crossref.org"
ROWS_PER_PAGE = 1000
MIN_REQUEST_INTERVAL = 1.1
MAX_RETRIES = 5
JOURNALS = {item['name']: dict(item, full_start=item['start_year'], full_end=item.get('end_year', 9999))
            for item in sources(collector='ieee_crossref', kind='journal')}
CICC_FULL_START = get_source('CICC')['start_year']
ICTA_FULL_START = get_source('ICTA')['start_year']
DEFAULT_SOURCES = tuple(item['name'] for item in sources(collector='ieee_crossref')
                        if publication_range(item, datetime.now().year, datetime.now().year - 1))
DOCUMENT_RE = re.compile(
    r"https?://(?:www\.)?ieeexplore\.ieee\.org/document/(\d+)/?",
    re.IGNORECASE,
)


def contact_email():
    value = (os.getenv("CROSSREF_MAILTO") or "").strip()
    return value if "@" in value else None


def retry_after_seconds(value, fallback):
    if value:
        try:
            return max(float(value), 0.0)
        except (TypeError, ValueError):
            try:
                target = parsedate_to_datetime(value)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=timezone.utc)
                return max((target - datetime.now(timezone.utc)).total_seconds(), 0.0)
            except (TypeError, ValueError, OverflowError):
                pass
    return float(fallback)


class CrossrefClient:
    def __init__(self, session=None, request_interval=MIN_REQUEST_INTERVAL):
        self.session = session or requests.Session()
        self.request_interval = request_interval
        self.last_request_started = None
        self.reported_limits = False
        user_agent = "IEEE_Paper_Server-IEEEMetadata/1.0"
        if contact_email():
            user_agent += f" (mailto:{contact_email()})"
        self.session.headers.update({"User-Agent": user_agent})

    def _wait_for_slot(self):
        if self.last_request_started is not None:
            remaining = self.request_interval - (time.monotonic() - self.last_request_started)
            if remaining > 0:
                time.sleep(remaining)
        self.last_request_started = time.monotonic()

    def get_message(self, endpoint, params):
        request_params = dict(params)
        if contact_email():
            request_params["mailto"] = contact_email()

        for attempt in range(1, MAX_RETRIES + 1):
            self._wait_for_slot()
            try:
                response = self.session.get(
                    f"{API_BASE}{endpoint}", params=request_params, timeout=90
                )
                if response.status_code == 403:
                    raise RuntimeError(
                        "Crossref가 요청을 차단했습니다(403). 자동 재시도를 중단합니다."
                    )
                if response.status_code == 429:
                    fallback = 2 ** attempt
                    delay = retry_after_seconds(response.headers.get("Retry-After"), fallback)
                    print(f"  [대기] Crossref 429: {delay:g}초 후 재시도 ({attempt}/{MAX_RETRIES})")
                    time.sleep(delay)
                    continue
                if 400 <= response.status_code < 500:
                    raise RuntimeError(f'Crossref rejected request ({response.status_code}); not retrying')
                response.raise_for_status()
                message = response.json()["message"]

                if not self.reported_limits:
                    limit = response.headers.get("X-Rate-Limit-Limit", "미제공")
                    interval = response.headers.get("X-Rate-Limit-Interval", "미제공")
                    concurrency = response.headers.get("X-Concurrency-Limit", "미제공")
                    print(
                        f"[*] Crossref 응답 제한: limit={limit}, interval={interval}, "
                        f"concurrency={concurrency}; 로컬 간격={self.request_interval:g}초"
                    )
                    self.reported_limits = True
                return message
            except RuntimeError:
                raise
            except (requests.RequestException, KeyError, ValueError) as exc:
                if attempt == MAX_RETRIES:
                    raise
                delay = 2 ** attempt
                print(
                    f"  [재시도] {type(exc).__name__}: {delay}초 후 재시도 "
                    f"({attempt}/{MAX_RETRIES})"
                )
                time.sleep(delay)
        raise RuntimeError("Crossref 요청 재시도 횟수를 초과했습니다.")


def format_authors(author_list):
    names = []
    for author in author_list or []:
        given = unescape(author.get("given") or "").strip()
        family = unescape(author.get("family") or author.get("name") or "").strip()
        full = f"{given} {family}".strip()
        if full:
            names.append(full)
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def extract_year(item):
    for key in ("published-print", "published-online", "published", "issued", "created"):
        node = item.get(key)
        parts = node.get("date-parts") if node else None
        if parts and parts[0] and parts[0][0]:
            return str(parts[0][0])
    return None


def clean_title(item):
    titles = item.get("title") or []
    if not titles or not isinstance(titles[0], str):
        return None
    title = re.sub(r"<[^>]+>", "", unescape(titles[0]))
    return " ".join(title.split()) or None


def extract_ieee_url(item):
    resource = item.get("resource") or {}
    primary = resource.get("primary") or {}
    url = (primary.get("URL") or "").strip()
    match = DOCUMENT_RE.fullmatch(url)
    if not match:
        return None
    return f"https://ieeexplore.ieee.org/document/{match.group(1)}/"


def extract_issue(item):
    volume = str(item.get("volume") or "").strip()
    issue = str(item.get("issue") or "").strip()
    page = str(item.get("page") or "").strip()
    parts = []
    if volume:
        parts.append(f"Vol. {volume}")
    if issue:
        parts.append(f"Issue {issue}")
    if page:
        parts.append(f"pp. {page}")
    return ", ".join(parts) or None


def item_to_journal_row(item, source_name):
    title = clean_title(item)
    authors = format_authors(item.get("author"))
    year = extract_year(item)
    url = extract_ieee_url(item)
    doi = (item.get("DOI") or "").strip()
    if not title or not authors or not year or not url or not doi:
        return None
    return {
        "Journal": source_name,
        "Year": year,
        "Issue": extract_issue(item),
        "Title": title,
        "Authors": authors,
        "URL": url,
        "DOI": doi,
    }


def journal_container_matches(item, config):
    """Aliases must not import a predecessor or another registered journal."""
    names = config.get('journal_titles')
    if not names:
        return True
    normalize = lambda value: re.sub(r'[^\w]+', '', unescape(value).casefold())
    allowed = {normalize(name) for name in names}
    return any(normalize(title) in allowed for title in item.get('container-title', []))


def fetch_journal(source_name, start_year, end_year, client):
    config = JOURNALS[source_name]
    high, low = max(start_year, end_year), min(start_year, end_year)
    rows_by_doi = {}
    excluded = 0
    # IEEE's old TCAS deposits retained predecessor ISSNs, and MWCL's print
    # and electronic records are not interchangeable Crossref endpoints.
    # Query all configured identities for the same window, then deduplicate.
    issns = list(dict.fromkeys([config['issn'], *config.get('issn_aliases', [])]))
    if config.get('issn_aliases') and not config.get('journal_titles'):
        raise ValueError(f'Journal ISSN aliases require an explicit title allowlist: {source_name}')
    for issn in issns:
        if not re.fullmatch(r'\d{4}-\d{3}[\dX]', issn):
            raise ValueError(f'Invalid journal ISSN alias: {issn}')
        audit, cursor, scanned, total_results = PageAudit(), '*', 0, None
        while True:
            params = {
                "filter": (
                    f"type:journal-article,from-pub-date:{low}-01-01,"
                    f"until-pub-date:{high}-12-31"
                ),
                "rows": ROWS_PER_PAGE,
                "cursor": cursor,
                "select": (
                    "DOI,title,author,volume,issue,page,container-title,ISSN,resource,published-print,"
                    "published-online,published,issued,created"
                ),
            }
            message = client.get_message(f"/journals/{issn}/works", params)
            next_cursor = audit.next_cursor(message, ROWS_PER_PAGE)
            if total_results is None:
                total_results = message.get("total-results", 0)
            items = message.get("items", [])
            scanned += len(items)
            for item in items:
                row = item_to_journal_row(item, source_name) if journal_container_matches(item, config) else None
                if row:
                    doi = row['DOI'].lower()
                    if doi in rows_by_doi and rows_by_doi[doi] != row:
                        raise RuntimeError(f'Conflicting metadata across journal ISSN endpoints: {doi}')
                    rows_by_doi[doi] = row
                else:
                    excluded += 1
            print(
                f"  [{source_name} ISSN {issn}] {min(scanned, total_results):,}/{total_results:,} 검사, "
                f"논문 {len(rows_by_doi):,}건, 제외 {excluded:,}건",
                end="\r", flush=True,
            )
            if not next_cursor:
                break
            cursor = next_cursor

    print(
        f"\n[{source_name} {low}~{high}] 완료: 논문 {len(rows_by_doi):,}건 "
        f"(비논문/삭제 DOI/필수필드 누락 제외 {excluded:,}건)"
    )
    return list(rows_by_doi.values())


def cicc_doi_match(doi, year):
    return bool(re.match(rf"^10\.1109/CICC\d*\.{int(year)}\.", doi or "", re.IGNORECASE))


def cicc_container_match(item):
    titles = item.get("container-title") or []
    return any("custom integrated circuits conference" in title.lower() for title in titles)


def item_to_cicc_row(item, requested_year):
    doi = (item.get("DOI") or "").strip()
    title = clean_title(item)
    authors = format_authors(item.get("author"))
    url = extract_ieee_url(item)
    if (
        not cicc_doi_match(doi, requested_year)
        or not cicc_container_match(item)
        or not title
        or not authors
        or not url
    ):
        return None
    return {
        "Conference": "CICC",
        "Year": str(requested_year),
        "Page": str(item.get("page") or "").strip() or None,
        "Title": title,
        "Authors": authors,
        "URL": url,
        "DOI": doi,
    }


def fetch_cicc_year(year, client):
    params = {
        "filter": (
            f"prefix:10.1109,type:proceedings-article,"
            f"from-pub-date:{year}-01-01,until-pub-date:{year}-12-31"
        ),
        "query.container-title": f"Custom Integrated Circuits Conference CICC {year}",
        "rows": ROWS_PER_PAGE,
        "select": (
            "DOI,title,author,page,container-title,resource,published-print,"
            "published-online,published,issued,created"
        ),
    }
    message = client.get_message("/works", params)
    items = message.get("items", [])
    strict_items = [
        item
        for item in items
        if cicc_doi_match(item.get("DOI"), year) and cicc_container_match(item)
    ]
    rows = [row for item in strict_items if (row := item_to_cicc_row(item, year))]
    excluded = len(strict_items) - len(rows)
    print(
        f"[CICC {year}] DOI 일치 {len(strict_items):,}건, "
        f"논문 {len(rows):,}건, 제외 {excluded:,}건"
    )
    return rows


def icta_doi_match(doi, year):
    """Match ICTA DOIs, including the CICTA prefix used by the 2018 event."""
    return bool(
        re.match(
            rf"^10\.1109/(?:CICTA|ICTA)\d*\.{int(year)}\.",
            doi or "",
            re.IGNORECASE,
        )
    )


def icta_container_match(item):
    titles = item.get("container-title") or []
    phrase = "international conference on integrated circuits, technologies and applications"
    return any(phrase in title.lower() for title in titles)


def item_to_icta_row(item, requested_year):
    doi = (item.get("DOI") or "").strip()
    title = clean_title(item)
    authors = format_authors(item.get("author"))
    url = extract_ieee_url(item)
    if (
        not icta_doi_match(doi, requested_year)
        or not icta_container_match(item)
        or not title
        or not authors
        or not url
    ):
        return None
    return {
        "Conference": "ICTA",
        "Year": str(requested_year),
        "Page": str(item.get("page") or "").strip() or None,
        "Title": title,
        "Authors": authors,
        "URL": url,
        "DOI": doi,
    }


def fetch_icta_year(year, client):
    params = {
        "filter": (
            f"prefix:10.1109,type:proceedings-article,"
            f"from-pub-date:{year}-01-01,until-pub-date:{year}-12-31"
        ),
        "query.container-title": (
            f"IEEE International Conference on Integrated Circuits "
            f"Technologies and Applications ICTA {year}"
        ),
        "rows": ROWS_PER_PAGE,
        "select": (
            "DOI,title,author,page,container-title,resource,published-print,"
            "published-online,published,issued,created"
        ),
    }
    message = client.get_message("/works", params)
    items = message.get("items", [])
    strict_items = [
        item
        for item in items
        if icta_doi_match(item.get("DOI"), year) and icta_container_match(item)
    ]
    rows = [row for item in strict_items if (row := item_to_icta_row(item, year))]
    excluded = len(strict_items) - len(rows)
    print(
        f"[ICTA {year}] DOI 일치 {len(strict_items):,}건, "
        f"논문 {len(rows):,}건, 제외 {excluded:,}건"
    )
    return rows


def year_range(start_year, end_year):
    high, low = max(start_year, end_year), min(start_year, end_year)
    return range(high, low - 1, -1)


def parse_sources(value):
    try:
        return [item['name'] for item in select_sources(sources(collector='ieee_crossref'), value)]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main():
    # Keep the old CLI as a metadata-only entry point, sharing validation,
    # exact-run output, registry selection and exit codes with the scheduler.
    from scripts.run_metadata_update import main as run_update
    return run_update(['--pipeline', 'ieee', '--collect-only', *sys.argv[1:]])


if __name__ == '__main__':
    raise SystemExit(main())
