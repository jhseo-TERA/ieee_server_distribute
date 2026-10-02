# -*- coding: utf-8 -*-
r"""OJSSC journal metadata collector using the public Crossref REST API.

Only metadata is collected. No library proxy, VPN, institutional login, or
PDF download is used. The generated Excel file is compatible with
``scripts/import_excel_to_db.py`` and is imported as:

    source_name=OJSSC, source_type=journal, source_system=ieee

Crossref also indexes covers, tables of contents, calls for papers, and other
front matter as ``journal-article``. Those records have no authors in OJSSC,
so this collector excludes authorless records from the paper index.

Examples:
  .venv\Scripts\python.exe scripts\fetch_ojssc_crossref.py
  .venv\Scripts\python.exe scripts\fetch_ojssc_crossref.py --full
"""
import argparse
from datetime import datetime
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
from scripts.metadata_sources import source

ISSN = source('OJSSC')['issn']
API_URL = f"https://api.crossref.org/journals/{ISSN}/works"
ROWS_PER_PAGE = 1000
REQUEST_SLEEP = 0.35
DEFAULT_FULL_START_YEAR = source('OJSSC')['start_year']
MAX_RETRIES = 5
DOCUMENT_RE = re.compile(r"https?://(?:www\.)?ieeexplore\.ieee\.org/document/(\d+)/?", re.IGNORECASE)


def contact_email():
    value = (os.getenv("CROSSREF_MAILTO") or "").strip()
    return value if "@" in value else None


def make_session():
    session = requests.Session()
    user_agent = "IEEE_Paper_Server-OJSSCMetadata/1.0"
    if contact_email():
        user_agent += f" (mailto:{contact_email()})"
    session.headers.update({"User-Agent": user_agent})
    return session


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


def extract_ieee_url(item):
    resource = item.get("resource") or {}
    primary = resource.get("primary") or {}
    url = primary.get("URL") or ""
    return url if DOCUMENT_RE.fullmatch(url.strip()) else None


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


def clean_title(item):
    titles = item.get("title") or []
    if not titles or not isinstance(titles[0], str):
        return None
    title = re.sub(r"<[^>]+>", "", unescape(titles[0]))
    return " ".join(title.split()) or None


def item_to_row(item):
    title = clean_title(item)
    authors = format_authors(item.get("author"))
    year = extract_year(item)
    url = extract_ieee_url(item)
    doi = (item.get("DOI") or "").strip()

    # OJSSC's authorless Crossref records are front matter (cover, TOC, CFP,
    # instructions, index) rather than papers suitable for recommendations.
    if not title or not authors or not year or not url or not doi:
        return None

    return {
        "Journal": "OJSSC",
        "Year": year,
        "Issue": extract_issue(item),
        "Title": title,
        "Authors": authors,
        "URL": url,
        "DOI": doi,
    }


def _get_json(session, params):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(API_URL, params=params, timeout=90)
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", attempt * 2))
                print(f"  [대기] Crossref 요청 제한(429), {retry_after:g}초 후 재시도")
                time.sleep(retry_after)
                continue
            response.raise_for_status()
            return response.json()["message"]
        except (requests.RequestException, KeyError, ValueError) as exc:
            if attempt == MAX_RETRIES:
                raise
            delay = attempt * 2
            print(f"  [재시도] {type(exc).__name__}: {delay}초 후 재시도 ({attempt}/{MAX_RETRIES})")
            time.sleep(delay)
    raise RuntimeError("Crossref 요청 재시도 횟수를 초과했습니다.")


def fetch_range(start_year, end_year, session):
    high, low = max(start_year, end_year), min(start_year, end_year)
    cursor = "*"
    rows_by_doi = {}
    scanned = 0
    excluded = 0
    total_results = None

    while True:
        params = {
            "filter": (
                f"type:journal-article,from-pub-date:{low}-01-01,"
                f"until-pub-date:{high}-12-31"
            ),
            "rows": ROWS_PER_PAGE,
            "cursor": cursor,
            "select": (
                "DOI,title,author,volume,issue,page,resource,published-print,"
                "published-online,published,issued,created"
            ),
        }
        if contact_email():
            params["mailto"] = contact_email()

        message = _get_json(session, params)
        if total_results is None:
            total_results = message.get("total-results", 0)

        items = message.get("items", [])
        if not items:
            break
        scanned += len(items)

        for item in items:
            row = item_to_row(item)
            if row:
                rows_by_doi[row["DOI"].lower()] = row
            else:
                excluded += 1

        print(
            f"  후보 {min(scanned, total_results):,}/{total_results:,} 검사, "
            f"논문 {len(rows_by_doi):,}건, 제외 {excluded:,}건",
            end="\r",
            flush=True,
        )

        next_cursor = message.get("next-cursor")
        if not next_cursor or len(items) < ROWS_PER_PAGE:
            break
        cursor = next_cursor
        time.sleep(REQUEST_SLEEP)

    print(
        f"\n[{low}~{high}] 완료: OJSSC 논문 {len(rows_by_doi):,}건 "
        f"(비논문/필수필드 누락 제외 {excluded:,}건)"
    )
    return list(rows_by_doi.values())


def main():
    now_year = datetime.now().year
    parser = argparse.ArgumentParser(description="OJSSC 메타데이터를 Crossref 공개 API로 수집")
    parser.add_argument(
        "--full",
        action="store_true",
        help=f"OJSSC 전체 발행 범위({DEFAULT_FULL_START_YEAR}년~현재)",
    )
    parser.add_argument("--start-year", type=int, default=now_year, help=f"시작 연도(기본 {now_year})")
    parser.add_argument("--end-year", type=int, default=now_year - 1, help=f"종료 연도(기본 {now_year - 1})")
    args = parser.parse_args()

    start_year = now_year if args.full else args.start_year
    end_year = DEFAULT_FULL_START_YEAR if args.full else args.end_year
    high, low = max(start_year, end_year), min(start_year, end_year)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(META_DIR, f"OJSSC_Crossref_{high}_{low}_{timestamp}.xlsx")
    os.makedirs(META_DIR, exist_ok=True)

    print("=" * 60)
    print(f"OJSSC 공개 메타데이터 수집 시작 ({low}~{high}년)")
    print("VPN/도서관 프록시/PDF 다운로드: 사용 안 함")
    print("저자 없는 표지/목차/CFP 등: 제외")
    print("=" * 60)

    rows = fetch_range(start_year, end_year, make_session())
    if not rows:
        raise SystemExit("[!] 수집된 OJSSC 논문 메타데이터가 없습니다.")

    pd.DataFrame(rows).sort_values(["Year", "Title"], ascending=[False, True]).to_excel(
        output_path, index=False
    )
    print(f"\n[완료] OJSSC 총 {len(rows):,}건. 파일: {output_path}")
    return output_path


if __name__ == "__main__":
    main()
