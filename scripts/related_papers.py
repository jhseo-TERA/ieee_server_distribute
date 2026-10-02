# -*- coding: utf-8 -*-
"""
주어진 논문 제목과 가장 유사한(FULLTEXT 매칭) 즐겨찾기 논문들을 찾는다.
zotero-ingest 스킬이 옵시디언 노트에 "Related Papers" 섹션을 채울 때 사용.

기존 /api/recommendations(웹서버)는 "즐겨찾기 전체의 키워드 프로파일" 기준으로
아직 안 즐겨찾기한 새 논문을 추천하는 반면, 이 스크립트는 반대로 "이 논문 한 편"과
비슷한 "이미 즐겨찾기된(=PDF/노트가 있을 가능성이 높은)" 논문을 찾는다 — 노트 간
상호 링크를 만들기 위한 용도라 즐겨찾기 범위로 한정한다.

사용:
  .venv\\Scripts\\python.exe scripts\\related_papers.py --title "제목" --exclude 1705376 --limit 5
  .venv\\Scripts\\python.exe scripts\\related_papers.py --title "제목" --limit 5 --format json
"""
import os
import sys
import json
import argparse

import pymysql
from dotenv import load_dotenv

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))

DB = dict(
    host=os.getenv("DB_HOST", "127.0.0.1"),
    port=int(os.getenv("DB_PORT", "3306")),
    user=os.getenv("DB_USER", "root"),
    password=os.getenv("DB_PASSWORD", ""),
    database=os.getenv("DB_NAME", "ieee_repo"),
    charset="utf8mb4",
)


def main():
    ap = argparse.ArgumentParser(description="이 논문과 비슷한 즐겨찾기 논문 찾기 (FULLTEXT)")
    ap.add_argument("--title", required=True, help="기준이 되는 논문 제목")
    ap.add_argument("--exclude", help="결과에서 제외할 article_number(자기 자신)")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown")
    args = ap.parse_args()

    conn = pymysql.connect(**DB)
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            sql = (
                "SELECT article_number, title, authors, year, source_name, source_system, "
                "MATCH(title, authors) AGAINST(%s IN NATURAL LANGUAGE MODE) AS score "
                "FROM papers "
                "WHERE is_favorite = 1 "
                "AND MATCH(title, authors) AGAINST(%s IN NATURAL LANGUAGE MODE) > 0"
            )
            params = [args.title, args.title]
            if args.exclude:
                sql += " AND article_number != %s"
                params.append(args.exclude)
            sql += " ORDER BY score DESC LIMIT %s"
            params.append(args.limit)
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()

    if args.format == "json":
        print(json.dumps(rows, default=str, ensure_ascii=False))
        return

    if not rows:
        print("_(관련 논문 없음)_")
        return
    for r in rows:
        print(f"- **{r['title']}** — {r['authors'] or '?'} ({r['year']}, {r['source_name']}) "
              f"[{r['source_system']}#{r['article_number']}]")


if __name__ == "__main__":
    main()
