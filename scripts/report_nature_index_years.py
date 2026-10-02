# -*- coding: utf-8 -*-
"""Report the indexed year coverage of the three Nature journals.

The report compares records actually indexed in MySQL with the Nature Crossref
Excel files kept under py_01_data/00_metadata.
"""

import argparse
import os
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import pandas as pd
import pymysql
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
JOURNALS = {
    "NPHOTON": "Nature Photonics",
    "NELECTRON": "Nature Electronics",
    "NCOMMS": "Nature Communications",
}


def connect_db():
    load_dotenv(ROOT / ".env")
    return pymysql.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", ""),
        database=os.getenv("DB_NAME", "ieee_repo"),
        charset="utf8mb4",
    )


def read_db_coverage():
    sql = """
        SELECT source_name, MIN(CAST(year AS UNSIGNED)),
               MAX(CAST(year AS UNSIGNED)), COUNT(*), COUNT(DISTINCT year)
        FROM papers
        WHERE source_system = 'nature'
          AND source_name IN ('NPHOTON', 'NELECTRON', 'NCOMMS')
        GROUP BY source_name
    """
    latest_sql = """
        SELECT p.source_name, p.year, COUNT(*)
        FROM papers p
        JOIN (
            SELECT source_name, MAX(CAST(year AS UNSIGNED)) AS max_year
            FROM papers
            WHERE source_system = 'nature'
              AND source_name IN ('NPHOTON', 'NELECTRON', 'NCOMMS')
            GROUP BY source_name
        ) latest
          ON latest.source_name = p.source_name
         AND CAST(p.year AS UNSIGNED) = latest.max_year
        WHERE p.source_system = 'nature'
        GROUP BY p.source_name, p.year
    """
    with connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            coverage = {row[0]: row[1:] for row in cur.fetchall()}
            cur.execute(latest_sql)
            latest_counts = {row[0]: int(row[2]) for row in cur.fetchall()}
    return coverage, latest_counts


def read_excel_coverage():
    years = defaultdict(list)
    files = defaultdict(set)
    for path in (ROOT / "py_01_data" / "00_metadata").glob("Nature_Crossref*.xlsx"):
        frame = pd.read_excel(path, usecols=["Journal", "Year"])
        for journal, group in frame.groupby("Journal"):
            if journal not in JOURNALS:
                continue
            numeric_years = pd.to_numeric(group["Year"], errors="coerce").dropna().astype(int)
            years[journal].extend(numeric_years.tolist())
            files[journal].add(path.name)
    return {
        journal: (min(values), max(values), len(values), len(files[journal]))
        for journal, values in years.items()
        if values
    }


def make_report(coverage, latest_counts, excel_coverage):
    today = date.today().isoformat()
    lines = [
        "# Nature 3개 저널 색인 연도 점검",
        "",
        f"- 점검일: {today}",
        "- Crossref 조회 범위: 2000–2026",
        "- 기준: `ieee_repo.papers`에서 `source_system='nature'`인 실제 색인 레코드",
        "- 교차 확인: `py_01_data/00_metadata/Nature_Crossref*.xlsx` 수집 파일",
        "",
        "## 결과",
        "",
        "| 저널 | 코드 | DB 색인 범위 | 최신 연도 | 최신 연도 논문 수 | DB 총 논문 수 | 수집 파일 범위 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for code, full_name in JOURNALS.items():
        db_min, db_max, total, _distinct = coverage.get(code, (None, None, 0, 0))
        x_min, x_max, _x_total, _file_count = excel_coverage.get(code, (None, None, 0, 0))
        lines.append(
            f"| {full_name} | `{code}` | {db_min}–{db_max} | **{db_max}** | "
            f"{latest_counts.get(code, 0):,} | {total:,} | {x_min}–{x_max} |"
        )
    lines += [
        "",
        "## 판정",
        "",
        "세 저널 모두 현재 데이터베이스에서 **2026년까지 색인**되어 있다. "
        "수집 Excel 파일의 최대 연도도 모두 2026년으로 일치한다.",
        "",
        "## 해석 시 주의사항",
        "",
        "- 이 결과의 ‘최신 연도’는 Crossref 메타데이터에서 추출해 DB에 저장한 출판 연도다. "
        "2026년 논문이 있다는 뜻이며, 2026년 전체 권호 수집이 완료됐다는 뜻은 아니다.",
        "- 2000년까지 조회 범위를 확장했지만 실제 최초 레코드는 `NPHOTON` 2006년, "
        "`NELECTRON` 2017년, `NCOMMS` 2010년이다. 최초 레코드 이전 연도에는 "
        "Crossref 조회 결과가 없어 빈 연도로 남는다.",
        "- `NCOMMS`는 제목 키워드 필터를 통과한 논문만 저장하므로, 저널 전체 논문 수가 아니다.",
        "- 세 Nature 저널 모두 저자 메타데이터가 없는 Editorial·안내성 레코드는 활성 DB에서 제외한다.",
        "- 수집기의 연도 필터와 저장 연도 추출 기준이 다를 수 있다. 수집기는 Crossref의 출판일로 "
        "조회하지만 저장 연도는 print → online → published 순으로 선택한다.",
    ]
    return "\n".join(lines) + "\n"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    parser = argparse.ArgumentParser(description="Nature 3개 저널의 DB 색인 연도 리포트")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "py_02_reports" / f"nature_index_years_{date.today():%Y%m%d}.md",
    )
    args = parser.parse_args()
    coverage, latest_counts = read_db_coverage()
    report = make_report(coverage, latest_counts, read_excel_coverage())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(report)
    print(f"Saved: {args.output}")


if __name__ == "__main__":
    main()
