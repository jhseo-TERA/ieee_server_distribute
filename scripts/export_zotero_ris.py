# -*- coding: utf-8 -*-
"""
즐겨찾기(★) 논문을 Zotero 가져오기용 RIS 파일로 내보내기.

DB(is_favorite=1) 전체를 대상으로 메타데이터를 RIS 레코드로 만들고,
PDF를 이미 보유한 건(pdf_available=1)은 L1(첨부 파일 링크) 필드를 추가해
Zotero가 Import 시 PDF를 함께 연결하도록 합니다. PDF가 없는 건은
메타데이터만 들어갑니다(나중에 PDF를 받은 뒤 다시 내보내면 됨).

- IEEE: TY=JOUR(저널)/CPAPER(학회), UR은 프록시 URL 대신
  https://ieeexplore.ieee.org/document/<번호>/ 로 재구성(로그인 없이 접근 가능).
- Optica(uri 키, 예 jlt-44-9-3341): TY=JOUR, UR은
  https://opg.optica.org/<약어>/abstract.cfm?uri=<키> 로 재구성.
- Nature(예 s41566-024-01426-x): TY=JOUR, UR은
  https://www.nature.com/articles/<키> 로 재구성.
소스 구분은 article_number 패턴 추론이 아니라 DB의 source_system 컬럼을 그대로 씀
(Nature 키도 Optica처럼 비숫자라 패턴만으론 구분이 안 됨).

사용:
  .venv\\Scripts\\python.exe scripts\\export_zotero_ris.py
  .venv\\Scripts\\python.exe scripts\\export_zotero_ris.py --out py_02_reports\\zotero_favorites.ris

Zotero에서: File > Import... > "A file (BibTeX, RIS, Zotero RDF, ...)" 선택 후
생성된 .ris 파일을 지정하면 즐겨찾기 항목과 PDF 첨부가 함께 들어옵니다.
"""
import os
import re
import argparse

import pymysql
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


def db_connect():
    return pymysql.connect(**DB)


def fetch_favorites(conn):
    sql = (
        "SELECT article_number, title, authors, year, source_name, "
        "source_type, source_system, issue, pdf_available, pdf_local_path "
        "FROM papers WHERE is_favorite = 1 "
        "ORDER BY (year+0) DESC, id DESC"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def split_authors(authors: str, source_system: str) -> list:
    """저자 문자열 분리.
    IEEE는 세미콜론(;) 구분, Optica/Nature는 쉼표(,) + 마지막 'and' 구분(옥스퍼드 콤마)."""
    authors = (authors or "").strip()
    if not authors:
        return []
    if source_system == "ieee":
        return [a.strip() for a in authors.split(";") if a.strip()]
    normalized = re.sub(r"\s+and\s+", ", ", authors)
    return [a.strip() for a in normalized.split(",") if a.strip()]


def clean_url(article_number: str, source_system: str) -> str:
    """프록시를 거치지 않는 원문 URL 재구성."""
    if source_system == "ieee":
        return f"https://ieeexplore.ieee.org/document/{article_number}/"
    if source_system == "nature":
        return f"https://www.nature.com/articles/{article_number}"
    if source_system == "designcon":
        return ""
    # optica: "<약어>-..." 형태의 uri 키 (예: jlt-44-9-3341, prj-14-3-731)
    seg = article_number.split("-", 1)[0]
    if seg == "optica":
        return f"https://opg.optica.org/abstract.cfm?uri={article_number}"
    return f"https://opg.optica.org/{seg}/abstract.cfm?uri={article_number}"


def build_record(row: dict) -> str:
    art = str(row["article_number"])
    source_system = row["source_system"]
    title = (row["title"] or "").strip()
    authors = split_authors(row["authors"], source_system)
    year = (row["year"] or "").strip()
    source = row["source_name"] or ""
    is_conf = (row["source_type"] == "conference")

    lines = []
    lines.append(f"TY  - {'CPAPER' if is_conf else 'JOUR'}")
    lines.append(f"TI  - {title}")
    for a in authors:
        lines.append(f"AU  - {a}")
    if year:
        lines.append(f"PY  - {year}")
    if source:
        lines.append(f"T2  - {source}")
        if is_conf:
            # Zotero는 Conference Paper의 "Conference Name"(T2 매핑)을 목록
            # 컬럼으로 노출하지 않음(알려진 제약). 실제 발행처(IEEE)는 전부
            # 동일해 구분력이 없으므로, 컬럼으로 보이는 Publisher(PB)에 학회명을
            # 대신 기록해 목록에서 바로 구분 가능하게 함.
            lines.append(f"PB  - {source}")
    if row.get("issue"):
        label = "Page" if is_conf else "Issue"
        lines.append(f"N1  - {label}: {row['issue']}")
    url = clean_url(art, source_system)
    if url:
        lines.append(f"UR  - {url}")
    lines.append(f"C7  - {art}")  # article number (조회용 참고 필드)

    if row["pdf_available"] and row.get("pdf_local_path"):
        abs_path = os.path.join(ROOT, row["pdf_local_path"].replace("/", os.sep))
        if os.path.isfile(abs_path):
            file_url = "file:///" + abs_path.replace("\\", "/")
            lines.append(f"L1  - {file_url}")

    lines.append("ER  - ")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="즐겨찾기 논문을 Zotero RIS로 내보내기")
    ap.add_argument("--out", default=os.path.join("py_02_reports", "zotero_favorites.ris"),
                     help="출력 파일 경로 (기본: py_02_reports/zotero_favorites.ris)")
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

    records = [build_record(r) for r in rows]
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(records) + "\n")

    with_pdf = sum(1 for r in rows if r["pdf_available"])
    print(f"[완료] 총 {len(rows)}건 내보냄 (PDF 첨부 포함 {with_pdf}건, 메타데이터만 {len(rows) - with_pdf}건)")
    print(f"       -> {out_path}")
    print("[*] Zotero: File > Import... > 'A file (BibTeX, RIS, Zotero RDF, ...)' 로 이 파일을 선택하세요.")


if __name__ == "__main__":
    main()
