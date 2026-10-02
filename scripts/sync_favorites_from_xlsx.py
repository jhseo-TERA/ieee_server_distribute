# -*- coding: utf-8 -*-
"""
export_favorites_xlsx.py 로 뽑은 뒤 외부에서 행을 지워서(관심 밖 논문 제거)
편집한 엑셀을, DB의 is_favorite 상태에 다시 반영.

정책: 엑셀 파일에 "남아있는" 번호(article_number) = 즐겨찾기 유지.
      기존에 즐겨찾기였는데 파일에서 빠진 번호 = 즐겨찾기 해제.
      (엑셀에 있는데 DB에서 즐겨찾기가 아닌 경우는 이상 케이스로만 보고, 건드리지 않음
       — 새로 즐겨찾기를 "추가"하는 용도가 아니라 "정리/축소"하는 용도의 동기화)

사용 (기본 모드 — 메인 시트에서 지워진 행을 해제로 반영):
  .venv\\Scripts\\python.exe scripts\\sync_favorites_from_xlsx.py favorites_scope_analysis.xlsx
  .venv\\Scripts\\python.exe scripts\\sync_favorites_from_xlsx.py favorites_scope_analysis.xlsx --apply

사용 (시트 지정 모드 — "범위외_후보"/"판단보류" 같은 별도 시트에 있는 논문을
직접 제외 대상으로 지정. 여러 시트는 쉼표로):
  .venv\\Scripts\\python.exe scripts\\sync_favorites_from_xlsx.py favorites_scope_analysis.xlsx --exclude-sheets 범위외_후보,판단보류
  .venv\\Scripts\\python.exe scripts\\sync_favorites_from_xlsx.py favorites_scope_analysis.xlsx --exclude-sheets 범위외_후보,판단보류 --apply

--apply 없이 실행하면 미리보기(뭐가 바뀔지)만 보여주고 실제로 DB는 안 바꿈.
"""
import os
import sys
import argparse

import pymysql
import pandas as pd
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


def load_excluded_from_sheets(path, sheet_names):
    """지정된 시트들에서 '번호' 컬럼을 모아 제외 대상 집합을 만든다."""
    combined = set()
    for sheet in sheet_names:
        df = pd.read_excel(path, sheet_name=sheet)
        if "번호" not in df.columns:
            sys.exit(f"[!] 시트 '{sheet}' 에 '번호' 컬럼이 없습니다.")
        keys = set(str(x) for x in df["번호"])
        print(f"[*] 시트 '{sheet}': {len(df)}행 / 고유 번호 {len(keys)}개")
        combined |= keys
    return combined


def main():
    ap = argparse.ArgumentParser(description="엑셀에서 지운(또는 특정 시트로 표시된) 논문을 DB 즐겨찾기 해제로 반영")
    ap.add_argument("xlsx_path", help="편집된 즐겨찾기 엑셀 파일 경로")
    ap.add_argument("--exclude-sheets", help="제외 대상 논문이 들어있는 시트명(쉼표구분). "
                                              "지정하면 메인 시트 diff 대신 이 시트들의 '번호'를 그대로 해제 대상으로 씀")
    ap.add_argument("--apply", action="store_true", help="실제로 DB에 반영(없으면 미리보기만)")
    args = ap.parse_args()

    path = args.xlsx_path if os.path.isabs(args.xlsx_path) else os.path.join(ROOT, args.xlsx_path)

    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT article_number FROM papers WHERE is_favorite=1")
            db_favs = set(str(r[0]) for r in cur.fetchall())
        print(f"[*] 현재 DB 즐겨찾기: {len(db_favs)}건")

        if args.exclude_sheets:
            sheet_names = [s.strip() for s in args.exclude_sheets.split(",") if s.strip()]
            excluded = load_excluded_from_sheets(path, sheet_names)
            to_unfav = sorted(excluded & db_favs)
            not_currently_fav = sorted(excluded - db_favs)
            print(f"\n[해제 대상] 지정 시트에 있고 현재 즐겨찾기인 논문: {len(to_unfav)}건")
            if not_currently_fav:
                print(f"[주의] 지정 시트에 있는데 이미 즐겨찾기가 아님(건드리지 않음): {len(not_currently_fav)}건")
        else:
            df = pd.read_excel(path)
            if "번호" not in df.columns:
                sys.exit("[!] '번호' 컬럼이 없습니다 — export_favorites_xlsx.py 로 뽑은 파일이 맞나요?")
            excel_keys = set(str(x) for x in df["번호"])
            print(f"[*] 엑셀 파일: {len(df)}행 / 고유 번호 {len(excel_keys)}개")

            to_unfav = sorted(db_favs - excel_keys)
            new_in_excel = sorted(excel_keys - db_favs)

            print(f"\n[해제 대상] 즐겨찾기였는데 엑셀에서 빠진 논문: {len(to_unfav)}건")
            if new_in_excel:
                print(f"[주의] 엑셀엔 있는데 DB에서 즐겨찾기가 아님(건드리지 않음): {len(new_in_excel)}건")
                for a in new_in_excel[:10]:
                    print(f"    {a}")

        if not to_unfav:
            print("\n[*] 해제할 게 없습니다. 이미 동기화된 상태입니다.")
            return

        if not args.apply:
            print("\n--- 미리보기 (해제될 논문, 상위 30건) ---")
            with conn.cursor() as cur:
                fmt = ",".join(["%s"] * len(to_unfav))
                cur.execute(
                    f"SELECT article_number, source_name, year, title FROM papers "
                    f"WHERE article_number IN ({fmt})", to_unfav)
                for art, src, year, title in cur.fetchall()[:30]:
                    print(f"  {src} {year} #{art} | {title[:60]}")
            print(f"\n[*] 미리보기만 했습니다. 실제 반영하려면 --apply 를 붙여서 다시 실행하세요.")
            return

        with conn.cursor() as cur:
            fmt = ",".join(["%s"] * len(to_unfav))
            cur.execute(f"UPDATE papers SET is_favorite=0 WHERE article_number IN ({fmt})", to_unfav)
            conn.commit()
            affected = cur.rowcount
        print(f"\n[완료] {affected}건 즐겨찾기 해제함.")

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM papers WHERE is_favorite=1")
            print(f"[*] 반영 후 DB 즐겨찾기 총: {cur.fetchone()[0]}건")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
