# -*- coding: utf-8 -*-
"""
즐겨찾기(★)가 아닌 논문의 PDF 파일을 전부 삭제하고 DB(pdf_available/pdf_local_path)를
초기화 — 즐겨찾기 논문만 남겨두고 나머지 PDF를 정리할 때 사용.

ieee-pdf/ · optica-pdf/ · nature-pdf/ 3개 폴더를 실제로 스캔해서(DB의
pdf_available 플래그만 믿지 않고), 각 파일의 article_number가 현재
is_favorite=1 이 아니면 삭제 대상으로 잡음 — 플래그가 어긋나 있던 파일도
놓치지 않음.

사용:
  .venv\\Scripts\\python.exe scripts\\reset_non_favorite_pdfs.py            # 미리보기만
  .venv\\Scripts\\python.exe scripts\\reset_non_favorite_pdfs.py --apply    # 실제 삭제+DB 반영
"""
import os
import sys
import argparse

import pymysql
from dotenv import load_dotenv

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))

PDF_DIRS = {
    "ieee": os.path.join(ROOT, "ieee-pdf"),
    "optica": os.path.join(ROOT, "optica-pdf"),
    "nature": os.path.join(ROOT, "nature-pdf"),
}

DB = dict(
    host=os.getenv("DB_HOST", "127.0.0.1"),
    port=int(os.getenv("DB_PORT", "3306")),
    user=os.getenv("DB_USER", "root"),
    password=os.getenv("DB_PASSWORD", ""),
    database=os.getenv("DB_NAME", "ieee_repo"),
    charset="utf8mb4",
)


def main():
    ap = argparse.ArgumentParser(description="즐겨찾기 아닌 논문의 PDF 삭제 + DB 초기화")
    ap.add_argument("--apply", action="store_true", help="실제로 삭제/반영(없으면 미리보기만)")
    args = ap.parse_args()

    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT article_number FROM papers WHERE is_favorite=1")
            fav_keys = set(str(r[0]) for r in cur.fetchall())
        print(f"[*] 현재 즐겨찾기: {len(fav_keys)}건 (이건 보존됨)")

        to_delete = {}  # publisher -> [(article_number, filepath), ...]
        total_keep = 0
        for pub, folder in PDF_DIRS.items():
            if not os.path.isdir(folder):
                continue
            files = [f for f in os.listdir(folder) if f.lower().endswith(".pdf")]
            dele = []
            for f in files:
                art = f[:-4]  # strip ".pdf"
                if art in fav_keys:
                    total_keep += 1
                else:
                    dele.append((art, os.path.join(folder, f)))
            to_delete[pub] = dele
            print(f"[*] {pub}-pdf: 전체 {len(files)}개 중 삭제 대상 {len(dele)}개 "
                  f"(즐겨찾기라 유지 {len(files) - len(dele)}개)")

        grand_total = sum(len(v) for v in to_delete.values())
        print(f"\n[요약] 삭제 대상 총 {grand_total}개 / 유지 {total_keep}개")

        if grand_total == 0:
            print("[*] 삭제할 파일이 없습니다.")
            return

        if not args.apply:
            print("\n--- 미리보기 (퍼블리셔별 상위 5개) ---")
            for pub, items in to_delete.items():
                for art, path in items[:5]:
                    print(f"  [{pub}] {art}")
            print("\n[*] 미리보기만 했습니다. 실제 삭제하려면 --apply 를 붙여서 다시 실행하세요.")
            return

        deleted = 0
        for pub, items in to_delete.items():
            for art, path in items:
                try:
                    os.remove(path)
                    deleted += 1
                except OSError as e:
                    print(f"[!] 삭제 실패 {path}: {e}")
        print(f"\n[완료] 파일 {deleted}개 삭제함.")

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE papers SET pdf_available=0, pdf_local_path=NULL "
                "WHERE is_favorite=0 AND pdf_available=1")
            conn.commit()
            print(f"[완료] DB pdf_available 초기화: {cur.rowcount}건")

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM papers WHERE pdf_available=1")
            print(f"[*] 반영 후 전체 PDF 보유: {cur.fetchone()[0]}건 (즐겨찾기 {len(fav_keys)}건 중 실제 파일 있는 것만)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
