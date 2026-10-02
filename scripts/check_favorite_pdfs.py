# -*- coding: utf-8 -*-
"""
즐겨찾기(★)된 논문들의 PDF가 실제로 IEEE/Optica/Nature/DesignCon 폴더에 있는지 점검.

DB의 pdf_available 플래그와 실제 파일 존재 여부를 대조해서 4가지로 분류:
  - 정상   : 플래그=1, 파일도 실제로 있음
  - 깨짐   : 플래그=1인데 파일이 없거나 너무 작음(손상) -> 재다운로드 필요
  - 어긋남 : 파일은 있는데 DB 플래그가 0으로 남아있음 -> import_excel_to_db.py 재실행하면 해결
  - 없음   : 파일도 없고 플래그도 0 -> download_pdfs.py / download_optica_pdfs.py 로 받아야 함

사용:
  .venv\\Scripts\\python.exe scripts\\check_favorite_pdfs.py
"""
import os
import sys
import pymysql
from dotenv import load_dotenv

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IEEE_PDF_DIR = os.path.join(ROOT, "ieee-pdf")
OPTICA_PDF_DIR = os.path.join(ROOT, "optica-pdf")
NATURE_PDF_DIR = os.path.join(ROOT, "nature-pdf")
DESIGNCON_PDF_DIR = os.path.join(ROOT, "DesignCon")
PDF_DIR_BY_SYSTEM = {
    "ieee": IEEE_PDF_DIR,
    "optica": OPTICA_PDF_DIR,
    "nature": NATURE_PDF_DIR,
    "designcon": DESIGNCON_PDF_DIR,
}
load_dotenv(os.path.join(ROOT, ".env"))

DB = dict(
    host=os.getenv("DB_HOST", "127.0.0.1"),
    port=int(os.getenv("DB_PORT", "3306")),
    user=os.getenv("DB_USER", "root"),
    password=os.getenv("DB_PASSWORD", ""),
    database=os.getenv("DB_NAME", "ieee_repo"),
    charset="utf8mb4",
)

MIN_SIZE = 2048  # 다른 다운로드 스크립트들과 동일한 "정상 파일" 최소 크기 기준


def pdf_path(
    article_number: str, source_system: str, pdf_local_path: str = ""
) -> str:
    if pdf_local_path:
        return os.path.join(
            ROOT, str(pdf_local_path).replace("/", os.sep).replace("\\", os.sep)
        )
    pdf_dir = PDF_DIR_BY_SYSTEM.get(source_system, OPTICA_PDF_DIR)
    return os.path.join(pdf_dir, f"{article_number}.pdf")


def file_ok(path: str) -> bool:
    io_path = os.path.abspath(path)
    if os.name == "nt" and not io_path.startswith("\\\\?\\"):
        if io_path.startswith("\\\\"):
            io_path = "\\\\?\\UNC\\" + io_path[2:]
        else:
            io_path = "\\\\?\\" + io_path
    return os.path.isfile(io_path) and os.path.getsize(io_path) > MIN_SIZE


def main():
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT article_number, source_name, year, LEFT(title,70), "
                "pdf_available, source_system, pdf_local_path "
                "FROM papers WHERE is_favorite=1 "
                "ORDER BY source_system, (year+0) DESC"
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        print("[*] 즐겨찾기된 논문이 없습니다.")
        return

    ok, broken, unsynced, missing = [], [], [], []
    for art, src, year, title, pdf_avail, source_system, pdf_local_path in rows:
        path = pdf_path(str(art), source_system, pdf_local_path)
        exists = file_ok(path)
        if pdf_avail and exists:
            ok.append((art, src, year, title))
        elif pdf_avail and not exists:
            broken.append((art, src, year, title, path))
        elif not pdf_avail and exists:
            unsynced.append((art, src, year, title, path))
        else:
            missing.append((art, src, year, title, source_system))

    total = len(rows)
    print("=" * 60)
    print(f"즐겨찾기 총 {total}건 점검 결과")
    print("=" * 60)
    print(f"[정상]   PDF 있음 + DB 플래그 일치 : {len(ok)}건")
    print(f"[깨짐]   DB엔 있다는데 파일 없음/손상 : {len(broken)}건")
    print(f"[어긋남] 파일은 있는데 DB 플래그 미갱신 : {len(unsynced)}건")
    print(f"[없음]   파일도 없고 다운로드 안 됨 : {len(missing)}건")
    print()

    if broken:
        print("--- [깨짐] 재다운로드 필요 ---")
        for art, src, year, title, path in broken:
            print(f"  {src} {year} #{art}  {title}")
            print(f"    -> {path}")
        print()

    if unsynced:
        print("--- [어긋남] import_excel_to_db.py 재실행하면 자동 해결 ---")
        for art, src, year, title, path in unsynced:
            print(f"  {src} {year} #{art}  {title}")
        print()

    if missing:
        n_ieee = sum(1 for *_, ss in missing if ss == "ieee")
        n_optica = sum(1 for *_, ss in missing if ss == "optica")
        n_nature = sum(1 for *_, ss in missing if ss == "nature")
        print(f"--- [없음] 총 {len(missing)}건 (IEEE {n_ieee} / Optica {n_optica} / Nature {n_nature}) ---")
        if n_ieee:
            print("  IEEE   -> .venv\\Scripts\\python.exe scripts\\run_pdf_download_routine.py --provider ieee")
        if n_optica:
            print("  Optica -> .venv\\Scripts\\python.exe scripts\\run_pdf_download_routine.py --provider optica")
        if n_nature:
            print("  Nature -> .venv\\Scripts\\python.exe scripts\\run_pdf_download_routine.py --provider nature")
        for art, src, year, title, ss in missing:
            print(f"  {src} {year} #{art}  {title}")
        print()

    print("=" * 60)
    if not broken and not unsynced and not missing:
        print("모든 즐겨찾기 PDF가 정상입니다.")


if __name__ == "__main__":
    main()
