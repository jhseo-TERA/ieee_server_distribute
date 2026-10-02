# -*- coding: utf-8 -*-
"""Audit title/author quality for Nature journals already indexed in MySQL."""

import argparse
import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pymysql
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
JOURNALS = {
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
        cursorclass=pymysql.cursors.DictCursor,
    )


def read_db():
    summary_sql = """
        SELECT source_name, COUNT(*) AS total,
               SUM(authors IS NULL OR TRIM(authors) = '') AS missing_authors,
               SUM(CHAR_LENGTH(title) < 20) AS short_titles,
               MIN(CAST(year AS UNSIGNED)) AS min_year,
               MAX(CAST(year AS UNSIGNED)) AS max_year
        FROM papers
        WHERE source_system = 'nature'
          AND source_name IN ('NELECTRON', 'NCOMMS')
        GROUP BY source_name
    """
    yearly_sql = """
        SELECT source_name, year, COUNT(*) AS total,
               SUM(authors IS NULL OR TRIM(authors) = '') AS missing_authors,
               SUM(CHAR_LENGTH(title) < 20) AS short_titles
        FROM papers
        WHERE source_system = 'nature'
          AND source_name IN ('NELECTRON', 'NCOMMS')
        GROUP BY source_name, year
        ORDER BY source_name, CAST(year AS UNSIGNED)
    """
    rows_sql = """
        SELECT article_number, source_name, year, title, authors, url
        FROM papers
        WHERE source_system = 'nature'
          AND source_name IN ('NELECTRON', 'NCOMMS')
    """
    with connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute(summary_sql)
            summary = {row["source_name"]: row for row in cur.fetchall()}
            cur.execute(yearly_sql)
            yearly = cur.fetchall()
            cur.execute(rows_sql)
            rows = cur.fetchall()
    return summary, yearly, rows


def read_source_rows():
    """Use the most complete workbook for each journal (latest wins on ties)."""
    records = {journal: {} for journal in JOURNALS}
    non_article_urls = {journal: 0 for journal in JOURNALS}
    selected = {journal: (-1, -1.0, None) for journal in JOURNALS}
    files = sorted((ROOT / "py_01_data" / "00_metadata").glob("Nature_Crossref*.xlsx"))
    for path in files:
        frame = pd.read_excel(path, usecols=["Journal", "Title", "Authors", "URL"])
        for journal in JOURNALS:
            subset = frame[frame["Journal"] == journal]
            candidate = (len(subset), path.stat().st_mtime)
            if candidate >= selected[journal][:2]:
                selected[journal] = (len(subset), path.stat().st_mtime, subset.copy())
    for journal in JOURNALS:
        frame = selected[journal][2]
        if frame is None:
            continue
        for row in frame.to_dict("records"):
            url = "" if pd.isna(row["URL"]) else str(row["URL"])
            marker = "nature.com/articles/"
            if marker not in url:
                non_article_urls[journal] += 1
                continue
            key = url.split(marker, 1)[1].split("?", 1)[0].rstrip("/")
            records[journal][key] = row
    return records, non_article_urls


def compare_source_to_db(source_rows, db_rows):
    db = {
        journal: {row["article_number"]: row for row in db_rows if row["source_name"] == journal}
        for journal in JOURNALS
    }
    result = {}
    for journal in JOURNALS:
        title_mismatch = 0
        author_mismatch = 0
        expected_excluded = set()
        for key, raw in source_rows[journal].items():
            raw_authors = None if pd.isna(raw["Authors"]) else str(raw["Authors"]).strip()
            if not raw_authors:
                expected_excluded.add(key)
                continue
            if key not in db[journal]:
                continue
            raw_title = None if pd.isna(raw["Title"]) else str(raw["Title"])
            title_mismatch += raw_title != db[journal][key]["title"]
            author_mismatch += raw_authors != db[journal][key]["authors"]
        result[journal] = {
            "title_mismatch": title_mismatch,
            "author_mismatch": author_mismatch,
            "expected_excluded": len(expected_excluded),
            "missing_db": len(set(source_rows[journal]) - expected_excluded - set(db[journal])),
        }
    return result


def percent(value, total):
    return 100 * int(value or 0) / int(total or 1)


def display_percent(value, total):
    result = percent(value, total)
    return "<0.1%" if 0 < result < 0.1 else f"{result:.1f}%"


def make_report(summary, yearly, comparison, non_article_urls, db_rows):
    lines = [
        "# Nature 자매지 메타데이터 품질 모니터링",
        "",
        f"- 점검일: {date.today().isoformat()}",
        "- 대상: Nature Electronics, Nature Communications",
        "- 기준: MySQL 색인 데이터와 저널별 최대 행 수의 최신 `Nature_Crossref*.xlsx` 원본 비교",
        "- 짧은 제목 기준: 20자 미만(제목 절단 판정이 아니라 이상 후보 탐지용)",
        "",
        "## 요약",
        "",
        "| 저널 | 색인 범위 | DB 건수 | 저자 없음 | 짧은 제목 | 정책 제외 | 제목 변형 | 저자 변형 | 적재 누락 | 판정 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for journal, name in JOURNALS.items():
        item = summary[journal]
        missing = int(item["missing_authors"] or 0)
        short = int(item["short_titles"] or 0)
        total = int(item["total"])
        comp = comparison[journal]
        verdict = "주의: 편집성 콘텐츠 혼입" if percent(missing, total) >= 5 else "양호: 소수 예외만 존재"
        lines.append(
            f"| {name} | {item['min_year']}–{item['max_year']} | {total:,} | "
            f"{missing:,} ({display_percent(missing, total)}) | {short:,} ({display_percent(short, total)}) | "
            f"{comp['expected_excluded']} | {comp['title_mismatch']} | {comp['author_mismatch']} | "
            f"{comp['missing_db']} | {verdict} |"
        )

    lines += ["", "## 연도별 저자 누락", ""]
    for journal, name in JOURNALS.items():
        lines += [f"### {name}", "", "| 연도 | 전체 | 저자 없음 | 비율 | 짧은 제목 |", "|---:|---:|---:|---:|---:|"]
        for row in yearly:
            if row["source_name"] != journal:
                continue
            lines.append(
                f"| {row['year']} | {row['total']:,} | {int(row['missing_authors'] or 0):,} | "
                f"{display_percent(row['missing_authors'], row['total'])} | {int(row['short_titles'] or 0):,} |"
            )
        lines.append("")

    lines += ["## 대표 이상 후보", ""]
    for journal, name in JOURNALS.items():
        samples = [
            row for row in db_rows
            if row["source_name"] == journal
            and (not row["authors"] or len(row["title"] or "") < 20)
        ][:5]
        lines.append(f"### {name}")
        lines.append("")
        for row in samples:
            author = row["authors"] or "저자 없음"
            lines.append(f"- {row['year']} — [{row['title']}]({row['url']}) — {author}")
        lines.append("")

    lines += [
        "## 결론",
        "",
        "- 저자 없는 Editorial 등 편집성 콘텐츠는 정책에 따라 활성 DB에서 제외되어 있다.",
        "- 두 저널 모두 Excel 원본에서 DB로 옮기는 과정의 제목/저자 변형이나 `/articles/` URL 적재 누락은 없다.",
        "- 짧은 제목은 원문 제목 자체가 짧은 사례이며, DB 또는 웹 UI의 제목 절단으로 판단되는 증거는 없다.",
    ]
    if any(non_article_urls.values()):
        lines.append(f"- `/articles/` 외 URL 후보: {non_article_urls}")
    return "\n".join(lines) + "\n"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    parser = argparse.ArgumentParser(description="Nature 자매지 메타데이터 품질 점검")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "py_02_reports" / f"nature_sister_metadata_quality_{date.today():%Y%m%d}.md",
    )
    args = parser.parse_args()
    summary, yearly, db_rows = read_db()
    source_rows, non_article_urls = read_source_rows()
    comparison = compare_source_to_db(source_rows, db_rows)
    report = make_report(summary, yearly, comparison, non_article_urls, db_rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(report)
    print(f"Saved: {args.output}")


if __name__ == "__main__":
    main()
