# -*- coding: utf-8 -*-
"""Quarantine authorless Nature records and remove them from active search."""

import argparse
import os
import sys
from datetime import date
from pathlib import Path

import pymysql
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
BACKUP_TABLE = f"papers_quarantine_nature_no_authors_{date.today():%Y%m%d}"
TARGET_WHERE = """
    source_system = 'nature'
    AND source_name IN ('NPHOTON', 'NELECTRON', 'NCOMMS')
    AND (authors IS NULL OR TRIM(authors) = '')
"""


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


def target_summary(cur):
    cur.execute(
        f"""SELECT source_name, COUNT(*), SUM(is_favorite=1), SUM(pdf_available=1)
             FROM papers WHERE {TARGET_WHERE} GROUP BY source_name ORDER BY source_name"""
    )
    return cur.fetchall()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    parser = argparse.ArgumentParser(description="저자 없는 Nature 레코드 격리 정제")
    parser.add_argument("--apply", action="store_true", help="백업 후 실제 정제 적용")
    args = parser.parse_args()

    conn = connect_db()
    try:
        with conn.cursor() as cur:
            rows = target_summary(cur)
            total = sum(int(row[1]) for row in rows)
            favorites = sum(int(row[2] or 0) for row in rows)
            pdfs = sum(int(row[3] or 0) for row in rows)
            print(f"대상: {total:,}건 | 즐겨찾기: {favorites:,}건 | PDF: {pdfs:,}건")
            for source, count, fav_count, pdf_count in rows:
                print(f"  {source}: {count:,}건 (즐겨찾기 {int(fav_count or 0):,}, PDF {int(pdf_count or 0):,})")

            if not args.apply:
                print("미적용 상태입니다. 적용하려면 --apply를 사용하세요.")
                return
            if favorites or pdfs:
                raise RuntimeError("즐겨찾기 또는 PDF 보유 레코드가 있어 안전을 위해 중단합니다.")
            if not total:
                print("정제할 레코드가 없습니다.")
                return

            cur.execute(f"CREATE TABLE IF NOT EXISTS `{BACKUP_TABLE}` LIKE papers")
            cur.execute(f"INSERT IGNORE INTO `{BACKUP_TABLE}` SELECT * FROM papers WHERE {TARGET_WHERE}")
            backed_up = cur.rowcount
            cur.execute(f"DELETE FROM papers WHERE {TARGET_WHERE}")
            deleted = cur.rowcount
            conn.commit()

            cur.execute(f"SELECT COUNT(*) FROM papers WHERE {TARGET_WHERE}")
            remaining = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(*) FROM `{BACKUP_TABLE}`")
            backup_total = cur.fetchone()[0]
            print(f"백업 신규 {backed_up:,}건 / 백업 테이블 합계 {backup_total:,}건")
            print(f"활성 DB 제거 {deleted:,}건 / 잔여 {remaining:,}건")
            print(f"복구 테이블: {BACKUP_TABLE}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
