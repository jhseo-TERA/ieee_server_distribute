# -*- coding: utf-8 -*-
"""
즐겨찾기(★) 논문 전체를 스프레드시트 관리용 Excel(.xlsx)로 내보내기.

scripts/export_zotero_ris.py 가 Zotero 가져오기용이라면, 이건 그냥
직접 열어서 정렬/필터/메모하면서 관리하기 좋은 형태로 뽑는 용도.
IEEE/Optica/Nature 모두 포함, URL은 로그인 필요한 연세대 프록시 대신 원문
링크(ieeexplore.ieee.org / opg.optica.org / nature.com)로 재구성해서 넣음.
소스 구분은 DB의 source_system 컬럼을 그대로 씀.

사용:
  .venv\\Scripts\\python.exe scripts\\export_favorites_xlsx.py
  .venv\\Scripts\\python.exe scripts\\export_favorites_xlsx.py --out C:\\Users\\ExampleUser\\Desktop\\favorites.xlsx
"""
import os
import argparse

import pymysql
import pandas as pd
from openpyxl.utils import get_column_letter
from dotenv import load_dotenv

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

TYPE_KO = {"journal": "저널", "conference": "학회"}


def db_connect():
    return pymysql.connect(**DB)


PUBLISHER_KO = {
    "ieee": "IEEE",
    "optica": "Optica",
    "nature": "Nature",
    "designcon": "DesignCon",
}


def fetch_favorites(conn):
    sql = (
        "SELECT article_number, title, authors, year, source_name, "
        "source_type, source_system, issue, pdf_available, pdf_local_path "
        "FROM papers WHERE is_favorite = 1 "
        "ORDER BY (year+0) DESC, source_name, id DESC"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def clean_url(article_number: str, source_system: str) -> str:
    """프록시를 거치지 않는 원문 URL 재구성 (export_zotero_ris.py 와 동일 로직)."""
    if source_system == "ieee":
        return f"https://ieeexplore.ieee.org/document/{article_number}/"
    if source_system == "nature":
        return f"https://www.nature.com/articles/{article_number}"
    if source_system == "designcon":
        return ""
    seg = article_number.split("-", 1)[0]
    if seg == "optica":
        return f"https://opg.optica.org/abstract.cfm?uri={article_number}"
    return f"https://opg.optica.org/{seg}/abstract.cfm?uri={article_number}"


def build_dataframe(rows):
    records = []
    for r in rows:
        art = str(r["article_number"])
        source_system = r["source_system"]
        records.append({
            "번호": art,
            "퍼블리셔": PUBLISHER_KO.get(source_system, source_system or ""),
            "제목": r["title"] or "",
            "저자": r["authors"] or "",
            "연도": r["year"] or "",
            "출처": r["source_name"] or "",
            "구분": TYPE_KO.get(r["source_type"], r["source_type"] or ""),
            "권호": r["issue"] or "",
            "원문 링크": clean_url(art, source_system),
            "PDF 보유": "있음" if r["pdf_available"] else "없음",
            "PDF 경로": r["pdf_local_path"] or "",
            "메모": "",
        })
    return pd.DataFrame(records)


def autofit_columns(writer, sheet_name, df):
    ws = writer.sheets[sheet_name]
    for i, col in enumerate(df.columns, start=1):
        max_len = max(
            [len(str(col))] + [len(str(v)) for v in df[col].astype(str)]
        )
        width = min(max(max_len + 2, 8), 80)
        # 제목/저자는 너무 넓어지지 않게 상한을 살짝 더 걸어줌
        if col in ("제목", "저자"):
            width = min(width, 55)
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"


def main():
    ap = argparse.ArgumentParser(description="즐겨찾기 논문을 관리용 Excel로 내보내기")
    ap.add_argument("--out", default=os.path.join("py_02_reports", "favorites.xlsx"),
                     help="출력 파일 경로 (기본: py_02_reports/favorites.xlsx)")
    args = ap.parse_args()

    out_path = args.out if os.path.isabs(args.out) else os.path.join(ROOT, args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    conn = db_connect()
    try:
        rows = fetch_favorites(conn)
    finally:
        conn.close()

    if not rows:
        print("[*] 즐겨찾기된 논문이 없습니다.")
        return

    df = build_dataframe(rows)
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="즐겨찾기")
        autofit_columns(writer, "즐겨찾기", df)

    n_pdf = sum(1 for r in rows if r["pdf_available"])
    print(f"[완료] 총 {len(rows)}건 내보냄 (PDF 보유 {n_pdf}건 / 미보유 {len(rows) - n_pdf}건)")
    print(f"       -> {out_path}")


if __name__ == "__main__":
    main()
