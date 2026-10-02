# -*- coding: utf-8 -*-
"""
Nature Photonics / Nature Communications 메타데이터를 Crossref REST API로 수집
— IEEE/Optica 수집 코드와는 완전히 분리된 별도 스크립트.

scripts/fetch_optica_crossref.py 와 동일한 방식(공식 Crossref API, 프록시/로그인/
캡차 불필요)이지만 대상 저널과 필터링 로직이 다름:

  - Nature Photonics(ISSN 1749-4885): 포토닉스 전문지라 필터 없이 전체 수집.
  - Nature Communications(ISSN 2041-1723): 전 분야를 다루는 종합 학술지라
    전체(9만+편)를 다 가져오면 관련없는 논문(생물/화학/의학 등)으로 코퍼스가
    희석됨. --keywords 로 지정한 단어가 제목에 하나라도 포함된 것만 남김
    (기본값: 포토닉스/광통신 관련 키워드).

키/URL 처리:
  - Crossref의 resource.primary.URL 이 그대로 https://www.nature.com/articles/<키>
    형태라 재구성 불필요.
  - 이 <키>(예: s41467-023-36307-4)는 IEEE(숫자)도 Optica(uri= 쿼리스트링) 패턴도
    아니라서, import_excel_to_db.py 에 Nature 전용 URL 인식 로직이 별도로 필요함
    (연동 시 함께 반영).

사용:
  # 전체 이력 (최초 1회)
  .venv\\Scripts\\python.exe scripts\\fetch_nature_crossref.py --full

  # 최근 구간만 (주기 업데이트용, 기본값: 올해~작년)
  .venv\\Scripts\\python.exe scripts\\fetch_nature_crossref.py

  # Nature Communications 키워드 직접 지정
  .venv\\Scripts\\python.exe scripts\\fetch_nature_crossref.py --full --keywords photonic,laser,waveguide
"""
import os
import sys
import time
import argparse
from datetime import datetime

import requests
import pandas as pd
from dotenv import load_dotenv

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from scripts.metadata_sources import sources, select_sources
from scripts.fetch_ieee_crossref import CrossrefClient, contact_email
from scripts.crossref_pagination import PageAudit

# source_name 은 기존 컬럼 관례(JSSC/OE/PR 처럼 약칭)를 따름.
JOURNALS = sources(collector='nature_crossref')

DEFAULT_KEYWORDS = [
    # 포토닉스/광통신
    "photonic", "photonics", "optical", "optics", "laser", "waveguide",
    "microring", "modulator", "nanophotonic", "plasmonic", "metasurface",
    "optoelectronic", "vcsel", "silicon photonics", "optical fiber",
    "optical communication", "wdm", "coherent optical", "lidar",
    # 회로설계/전자공학 (IEEE 쪽 관심사와 겹치는 Nature Communications 논문도 채택)
    "circuit", "cmos", "transceiver", "transistor", "semiconductor",
    "integrated circuit", "wireless", "amplifier", "sensor chip",
    "analog-to-digital", "digital-to-analog", "wearable electronics",
    "flexible electronics", "neuromorphic",
]

API_BASE = "https://api.crossref.org"
ROWS_PER_PAGE = 1000
REQUEST_SLEEP = 0.3


def make_session():
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": f"IEEE_Paper_Server-NatureSweep/1.0 (mailto:{contact_email()})"
    })
    return sess


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


def extract_url(item):
    doi = (item.get("DOI") or "").strip()
    if doi.lower().startswith("10.1038/"):
        return f"https://www.nature.com/articles/{doi.split('/', 1)[1]}"
    resource = item.get("resource") or {}
    primary = resource.get("primary") or {}
    url = primary.get("URL")
    if url:
        return url
    return f"https://doi.org/{doi}" if doi else None


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


def title_matches(title, keywords):
    t = title.lower()
    return any(kw.lower() in t for kw in keywords)


def fetch_journal(journal, start_year, end_year, session, keywords, on_batch):
    audit = PageAudit()
    issn = journal["issn"]
    name = journal["name"]
    do_filter = journal["filter_keywords"]
    filters = []
    if start_year and end_year:
        lo, hi = sorted([int(start_year), int(end_year)])
        filters.append(f"from-pub-date:{lo}-01-01")
        filters.append(f"until-pub-date:{hi}-12-31")

    cursor = "*"
    total_fetched = 0
    total_kept = 0
    total_skipped_no_authors = 0
    total_results = None
    client = session if isinstance(session, CrossrefClient) else CrossrefClient(session=session)

    while True:
        params = {
            "rows": ROWS_PER_PAGE,
            "cursor": cursor,
            "select": "DOI,title,author,volume,issue,resource,published-print,published-online,published,issued,created",
        }
        if filters:
            params["filter"] = ",".join(filters)

        data = client.get_message(f'/journals/{issn}/works', params)
        next_cursor = audit.next_cursor(data, ROWS_PER_PAGE)

        if total_results is None:
            total_results = data.get("total-results", 0)
            print(f"[{name}] 전체 {total_results:,}건 대상"
                  + (f" (제목에 키워드 포함된 것만 필터링)" if do_filter else ""))

        items = data.get("items", [])
        if not items:
            break

        rows = []
        for item in items:
            titles = item.get("title") or []
            title = titles[0] if titles else None
            if not title:
                continue
            if do_filter and not title_matches(title, keywords):
                continue
            authors = format_authors(item.get("author"))
            if not authors:
                total_skipped_no_authors += 1
                continue
            rows.append({
                "Journal": name,
                "Year": extract_year(item),
                "Category": extract_category(item),
                "Title": title,
                "Authors": authors,
                "URL": extract_url(item),
                "DOI": item.get('DOI'),
            })

        total_fetched += len(items)
        total_kept += len(rows)
        if rows:
            on_batch(rows)
        suffix = f" (채택 {total_kept:,})" if do_filter else ""
        print(f"  [{name}] {total_fetched:,}/{total_results:,} 조회{suffix}...", end="\r", flush=True)

        if not next_cursor:
            break
        cursor = next_cursor
        time.sleep(REQUEST_SLEEP)

    print(f"\n[{name}] 완료: 조회 {total_fetched:,}건 / 채택 {total_kept:,}건"
          f" / 저자 없음 제외 {total_skipped_no_authors:,}건")
    return total_kept


def main():
    now_year = datetime.now().year
    ap = argparse.ArgumentParser(description="Nature Photonics/Electronics/Communications 메타데이터를 Crossref API로 수집")
    ap.add_argument("--full", action="store_true", help="전체 이력(연도 필터 없음)")
    ap.add_argument("--start-year", type=int, default=now_year, help=f"시작 연도(최신, 기본 {now_year})")
    ap.add_argument("--end-year", type=int, default=now_year - 1, help=f"종료 연도(과거, 기본 {now_year - 1})")
    ap.add_argument("--journals", help="쉼표로 구분된 저널명 부분집합 (예: NPHOTON,NCOMMS). 기본: 전체 3개")
    ap.add_argument("--keywords", help="Nature Communications 제목 필터 키워드(쉼표구분). 기본 내장 목록 사용")
    args = ap.parse_args()

    try:
        journals = select_sources(JOURNALS, args.journals)
    except ValueError as exc:
        ap.error(str(exc))

    keywords = [k.strip() for k in args.keywords.split(",")] if args.keywords else DEFAULT_KEYWORDS
    start_year, end_year = (None, None) if args.full else (args.start_year, args.end_year)

    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    range_tag = "full" if args.full else f"{args.start_year}_{args.end_year}"
    file_name = f"Nature_Crossref_{range_tag}_{now_str}.xlsx"
    output_path = os.path.join(ROOT, "py_01_data", "00_metadata", file_name)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    print("=" * 50)
    print(f"Crossref 기반 Nature 수집 시작 ({'전체 이력' if args.full else f'{end_year}~{start_year}년'})")
    print(f"대상 저널: {', '.join(j['name'] for j in journals)}")
    if any(j["filter_keywords"] for j in journals):
        print(f"NCOMMS 필터 키워드: {', '.join(keywords)}")
    print("=" * 50)

    session = make_session()
    all_rows = []

    def on_batch(rows):
        all_rows.extend(rows)
        if all_rows:
            pd.DataFrame(all_rows).to_excel(output_path, index=False)

    grand_total = 0
    failures = []
    for journal in journals:
        try:
            count = fetch_journal(journal, start_year, end_year, session, keywords, on_batch)
            if not count:
                raise RuntimeError('No papers collected for this source')
            grand_total += count
        except Exception as e:
            failures.append(journal['name'])
            print(f"\n[주의] [{journal['name']}] 처리 중 오류로 건너뜁니다: {type(e).__name__}: {e}")
            continue

    if all_rows:
        pd.DataFrame(all_rows).to_excel(output_path, index=False)

    if failures or not all_rows:
        raise RuntimeError(f'Incomplete Nature collection: {failures}')

    print(f"\n[완료] 총 {grand_total:,}건 수집. 파일: {output_path}")
    return output_path


if __name__ == "__main__":
    main()
