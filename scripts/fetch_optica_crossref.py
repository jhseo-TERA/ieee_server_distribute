# -*- coding: utf-8 -*-
"""
Optica 저널(Optics Express/Optics Letters/Photonics Research/Optica/JLT) 메타데이터를
Crossref REST API로 수집 — 프록시/로그인/Selenium/캡차가 전혀 필요 없음.

배경: py_00_src/proxy_access_test_v28_Optica.py 는 연세대 프록시로 실제
opg.optica.org 를 스크래핑하는 방식이라 봇 탐지(캡차)에 걸리고, 무인 자동화가
어려움. 반면 이 논문들은 모두 Crossref(DOI 등록기관)에 메타데이터가 등록돼
있고, Crossref REST API(api.crossref.org)는 공개·무인증 API라서 캡차 문제
자체가 구조적으로 없음. 요청은 공용 API 제한보다 낮은 속도로 순차 실행함.

핵심 필드 매핑 (Crossref work 객체 -> 우리 스키마):
  - title[0]                                  -> Title
  - author[].given + family                   -> Authors ("A, B, and C" 형식,
                                                  기존 Optica 스크래핑 데이터와
                                                  동일한 포맷 유지)
  - published-print/online/issued 의 연도     -> Year
  - "Vol. {volume}, Issue {issue}"             -> Category (OSA 자체 주제분류는
                                                  Crossref에 없어 이걸로 대체)
  - resource.primary.URL                       -> URL (예: https://opg.optica.org
                                                  /oe/abstract.cfm?uri=oe-18-26-27481)
    -> import_excel_to_db.py 의 URI_RE 가 그대로 article_number(uri 키)를
       추출하므로, 기존 Selenium 스크래핑으로 쌓인 DB 레코드와 동일한 키로
       매칭되어 upsert 시 중복 없이 병합됨.

출력 컬럼은 기존 형식에 DOI와 IEEEArticleNumber를 추가하며,
scripts/import_excel_to_db.py가 JLT에 한해 DOI 기반 안정 키로 적재함.

사용:
  # 전체 이력 (최초 1회, 5개 저널)
  .venv\\Scripts\\python.exe scripts\\fetch_optica_crossref.py --full

  # 최근 구간만 (주기 업데이트용, 기본값: 올해~작년)
  .venv\\Scripts\\python.exe scripts\\fetch_optica_crossref.py
  .venv\\Scripts\\python.exe scripts\\fetch_optica_crossref.py --start-year 2026 --end-year 2025
"""
import os
import re
import sys
import argparse
from datetime import datetime

import pandas as pd
from dotenv import load_dotenv

try:
    from scripts.fetch_ieee_crossref import CrossrefClient
except ModuleNotFoundError:
    from fetch_ieee_crossref import CrossrefClient

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from scripts.metadata_sources import sources, select_sources
from scripts.crossref_pagination import PageAudit

JOURNALS = sources(collector='optica_crossref')

API_BASE = "https://api.crossref.org"
ROWS_PER_PAGE = 1000
IEEE_DOCUMENT_RE = re.compile(r"ieeexplore\.ieee\.org/document/(\d+)", re.IGNORECASE)


def format_authors(author_list):
    names = []
    for a in author_list or []:
        given = (a.get("given") or "").strip()
        family = (a.get("family") or a.get("name") or "").strip()
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
        if node and node.get("date-parts") and node["date-parts"][0]:
            y = node["date-parts"][0][0]
            if y:
                return str(y)
    return None


def extract_category(item):
    volume = item.get("volume")
    issue = item.get("issue")
    if volume and issue:
        return f"Vol. {volume}, Issue {issue}"
    if volume:
        return f"Vol. {volume}"
    return None


def extract_url(item, journal_name=None):
    # JLT는 IEEE가 Crossref DOI 등록기관이라 resource.primary.URL이
    # ieeexplore.ieee.org를 가리킴 - 그대로 쓰면 import_excel_to_db.py의
    # URL 도메인 기반 분류에서 source_system='ieee'로 잘못 잡혀서, 이미
    # 있는 jlt-<vol>-<issue>-<page> 키 체계(source_system='optica')와
    # 어긋나 버림(실제로 한 번 이 문제로 데이터가 쪼개짐). volume/issue/page로
    # opg.optica.org 스타일 URL을 직접 만들어서 우회.
    if journal_name == "JLT":
        volume, issue = item.get("volume"), item.get("issue")
        page = (item.get("page") or "").split("-")[0]
        if volume and issue and page:
            return f"https://opg.optica.org/jlt/abstract.cfm?uri=jlt-{volume}-{issue}-{page}"

    resource = item.get("resource") or {}
    primary = resource.get("primary") or {}
    url = primary.get("URL")
    if url:
        return url
    # 백업: DOI 링크 (uri 추출은 안 되지만 최소한 논문 위치는 확인 가능)
    doi = item.get("DOI")
    return f"https://doi.org/{doi}" if doi else None


def extract_ieee_document_id(item):
    resource = item.get("resource") or {}
    primary = resource.get("primary") or {}
    match = IEEE_DOCUMENT_RE.search(primary.get("URL") or "")
    return match.group(1) if match else None


def fetch_journal(journal, start_year, end_year, client, on_batch):
    """journal 하나를 cursor 페이지네이션으로 끝까지 순회하며 on_batch(rows) 호출."""
    issn = journal["issn"]
    audit = PageAudit()
    name = journal["name"]
    filters = []
    if start_year and end_year:
        lo, hi = sorted([int(start_year), int(end_year)])
        filters.append(f"from-pub-date:{lo}-01-01")
        filters.append(f"until-pub-date:{hi}-12-31")

    cursor = "*"
    total_fetched = 0
    total_results = None

    while True:
        params = {
            "rows": ROWS_PER_PAGE,
            "cursor": cursor,
            "select": "DOI,title,author,volume,issue,page,resource,published-print,published-online,published,issued,created",
        }
        if filters:
            params["filter"] = ",".join(filters)

        data = client.get_message(f"/journals/{issn}/works", params)
        next_cursor = audit.next_cursor(data, ROWS_PER_PAGE)

        if total_results is None:
            total_results = data.get("total-results", 0)
            print(f"[{name}] 전체 {total_results:,}건 대상")

        items = data.get("items", [])
        if not items:
            break

        rows = []
        for item in items:
            titles = item.get("title") or []
            title = titles[0] if titles else None
            if not title:
                continue
            authors = format_authors(item.get("author"))
            if name == "JLT" and not authors:
                # Crossref classifies covers, tables of contents, publication
                # information, and similar front matter as journal articles.
                continue
            rows.append({
                "Journal": name,
                "Year": extract_year(item),
                "Category": extract_category(item),
                "Title": title,
                "Authors": authors,
                "URL": extract_url(item, journal_name=name),
                "DOI": item.get("DOI"),
                "IEEEArticleNumber": extract_ieee_document_id(item),
            })

        total_fetched += len(rows)
        on_batch(rows)
        print(f"  [{name}] {total_fetched:,}/{total_results:,} 수집...", end="\r", flush=True)

        if not next_cursor:
            break
        cursor = next_cursor

    print(f"\n[{name}] 완료: {total_fetched:,}건")
    return total_fetched


def main():
    now_year = datetime.now().year
    ap = argparse.ArgumentParser(description="Optica 저널 메타데이터를 Crossref API로 수집")
    ap.add_argument("--full", action="store_true", help="전체 이력(연도 필터 없음)")
    ap.add_argument("--start-year", type=int, default=now_year, help=f"시작 연도(최신, 기본 {now_year})")
    ap.add_argument("--end-year", type=int, default=now_year - 1, help=f"종료 연도(과거, 기본 {now_year - 1})")
    ap.add_argument("--journals", help=f"쉼표로 구분된 저널명 부분집합 (예: OE,OL). 기본: 전체 {len(JOURNALS)}개")
    args = ap.parse_args()

    try:
        journals = select_sources(JOURNALS, args.journals)
    except ValueError as exc:
        ap.error(str(exc))

    start_year, end_year = (None, None) if args.full else (args.start_year, args.end_year)

    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    range_tag = "full" if args.full else f"{args.start_year}_{args.end_year}"
    file_name = f"Optica_Crossref_{range_tag}_{now_str}.xlsx"
    output_path = os.path.join(ROOT, "py_01_data", "00_metadata", file_name)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    print("=" * 50)
    print(f"Crossref 기반 Optica 수집 시작 ({'전체 이력' if args.full else f'{end_year}~{start_year}년'})")
    print(f"대상 저널: {', '.join(j['name'] for j in journals)}")
    print("=" * 50)

    client = CrossrefClient()
    all_rows = []

    def on_batch(rows):
        all_rows.extend(rows)
        if all_rows:
            pd.DataFrame(all_rows).to_excel(output_path, index=False)

    grand_total = 0
    failures = []
    for journal in journals:
        try:
            count = fetch_journal(journal, start_year, end_year, client, on_batch)
            if not count:
                raise RuntimeError('No papers collected for this source')
            grand_total += count
        except Exception as e:
            failures.append(journal['name'])
            print(f"\n[주의] [{journal['name']}] 처리 중 오류로 건너뜁니다: {type(e).__name__}: {e}")
            continue

    if all_rows:
        pd.DataFrame(all_rows).to_excel(output_path, index=False)

    if failures:
        raise RuntimeError(f'Incomplete Optica collection: {failures}')
    if not all_rows:
        print("[!] 수집된 Optica 논문 메타데이터가 없습니다.")
        return None

    print(f"\n[완료] 총 {grand_total:,}건 수집. 파일: {output_path}")
    return output_path


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
