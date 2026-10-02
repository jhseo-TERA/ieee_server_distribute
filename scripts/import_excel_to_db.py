# -*- coding: utf-8 -*-
"""
Excel(py_01_data/00_metadata/*.xlsx) -> MySQL(ieee_repo.papers) import.

- 저널/학회/Optica/Nature 파일의 컬럼 차이를 자동 판별
- IEEE URL(/document/<id>), Optica URL(?uri=<key>), Nature URL(nature.com/articles/<key>)
  에서 고유키 추출
- ieee-pdf/ (IEEE) · optica-pdf/ (Optica) · nature-pdf/ (Nature) 에 PDF 존재 여부로
  pdf_available 세팅
- ON DUPLICATE KEY UPDATE 로 중복 실행 안전
- DB 접속정보는 .env 에서 로드

실행:  .venv/Scripts/python.exe scripts/import_excel_to_db.py
"""
import os
import re
import glob
import math
import argparse
import sys
import hashlib
from urllib.parse import parse_qs, urlparse
import pymysql
from dotenv import load_dotenv

# ---------- 경로 ----------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
META_DIR = os.path.join(ROOT, "py_01_data", "00_metadata")
IEEE_PDF_DIR = os.path.join(ROOT, "ieee-pdf")      # IEEE(숫자 키) PDF
OPTICA_PDF_DIR = os.path.join(ROOT, "optica-pdf")  # Optica(uri 키) PDF
NATURE_PDF_DIR = os.path.join(ROOT, "nature-pdf")  # Nature(articles/<키>) PDF

load_dotenv(os.path.join(ROOT, ".env"))

DB = dict(
    host=os.getenv("DB_HOST", "127.0.0.1"),
    port=int(os.getenv("DB_PORT", "3306")),
    user=os.getenv("DB_USER", "root"),
    password=os.getenv("DB_PASSWORD", ""),
    database=os.getenv("DB_NAME", "ieee_repo"),
    charset="utf8mb4",
)

DOC_RE = re.compile(r"/document/(\d+)")
URI_RE = re.compile(r"[?&]uri=([\w\-.]+)", re.IGNORECASE)
NATURE_RE = re.compile(r"nature\.com/articles/([\w.\-]+)", re.IGNORECASE)
NATURE_DOI_RE = re.compile(
    r"(?:nature\.com/doifinder/|doi\.org/)?10\.1038/([\w.\-]+)", re.IGNORECASE
)
JLT_DOI_RE = re.compile(r"^10\.1109/(?:JLT\.|50\.)", re.IGNORECASE)

PDF_DIR_BY_KIND = {"ieee": IEEE_PDF_DIR, "optica": OPTICA_PDF_DIR, "nature": NATURE_PDF_DIR}


def clean(val):
    """NaN/None -> None, 그 외 -> 공백정리된 str."""
    if val is None:
        return None
    if isinstance(val, float) and math.isnan(val):
        return None
    s = str(val).strip()
    if s == "" or s.lower() == "nan":
        return None
    return s


def norm_year(val):
    s = clean(val)
    if s is None:
        return None
    # 2026.0 -> 2026
    m = re.match(r"^(\d{4})", s)
    return m.group(1) if m else s[:10]


def normalize_publication_source(source_name, year):
    """Keep the pre/post-2023 microwave letters titles as separate sources."""
    if source_name not in {"MWCL", "MWTL"}:
        return source_name
    try:
        return "MWCL" if int(year) <= 2022 else "MWTL"
    except (TypeError, ValueError):
        return source_name


def extract_key(url):
    """(article_number, kind) 반환. kind in {'ieee','optica','nature',None}."""
    if not url:
        return None, None
    m = DOC_RE.search(url)
    if m:
        return m.group(1), "ieee"
    m = NATURE_RE.search(url)
    if m:
        return m.group(1), "nature"
    m = NATURE_DOI_RE.search(url)
    if m:
        return m.group(1), "nature"
    m = URI_RE.search(url)
    if m:
        return m.group(1), "optica"
    parsed = urlparse(url)
    if (parsed.hostname or '').lower() in {'opg.optica.org', 'www.opg.optica.org'}:
        query = {key.lower(): values for key, values in parse_qs(parsed.query).items()}
        values = query.get('doi', [])
        if values and (key := optica_doi_key(values[0])):
            return key, 'optica'
    return None, None


def jlt_doi_key(doi):
    """Return a filesystem-safe, stable key for Crossref JLT records."""
    value = clean(doi)
    if not value:
        return None
    value = value.lower()
    if not JLT_DOI_RE.match(value):
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return f"jlt-doi-{slug}"[:80]


def optica_doi_key(doi):
    """Stable fallback for OPG early-access URLs that have doi= but no uri=."""
    value = (clean(doi) or '').lower()
    if not re.fullmatch(r'10\.1364/[a-z0-9._-]+', value):
        return None
    slug = re.sub(r'[^a-z0-9]+', '-', value)[:50]
    digest = hashlib.sha256(value.encode()).hexdigest()[:12]
    return f'optica-doi-{slug}-{digest}'


def reconcile_doi_keys(rows, conn):
    """Reuse existing Optica IDs when an early-access DOI later gets a URI."""
    dois = sorted({row[11].lower() for row in rows if row[6] == 'optica' and row[11]})
    existing = {}
    with conn.cursor() as cur:
        for offset in range(0, len(dois), 1000):
            chunk = dois[offset:offset + 1000]
            placeholders = ','.join(['%s'] * len(chunk))
            cur.execute(f'SELECT doi,article_number FROM papers WHERE source_system=%s '
                        f'AND doi IN ({placeholders})', ['optica', *chunk])
            for doi, key in cur.fetchall():
                normalized = doi.lower()
                if normalized in existing and existing[normalized] != key:
                    raise ValueError(f'Duplicate existing Optica DOI requires review: {doi}')
                existing[normalized] = key
    db_dois = set(existing)
    for row in rows:
        if row[6] == 'optica' and row[11]:
            doi = row[11].lower()
            # Within a new batch prefer a publisher URI over a fallback DOI key.
            if doi not in existing or (doi not in db_dois
                                      and existing[doi].startswith('optica-doi-')
                                      and not row[0].startswith('optica-doi-')):
                existing[doi] = row[0]
    unique = {}
    for row in rows:
        key = existing.get(row[11].lower(), row[0]) if row[6] == 'optica' and row[11] else row[0]
        updated = (key, *row[1:])
        if key in unique and unique[key][4:7] != row[4:7]:
            raise ValueError(f'Conflicting publication ownership for {key}')
        unique[key] = updated
    return list(unique.values())


def pick(colmap, row, *names):
    for n in names:
        if n in colmap:
            return row[colmap[n]]
    return None


def load_rows(files=None, strict=False):
    """Read explicit run files, or the legacy root directory when omitted."""
    import pandas as pd

    files = list(files) if files is not None else glob.glob(os.path.join(META_DIR, '*.xlsx'))
    files = sorted(files, key=lambda path: (os.path.getmtime(path), str(path)))
    if strict and not files:
        raise ValueError('No metadata files selected')
    print(f"[*] metadata 파일 {len(files)}개 발견")

    records = {}          # article_number -> tuple(record)
    stats = {
        "read": 0,
        "no_title": 0,
        "no_key": 0,
        "nature_no_authors": 0,
        "legacy_jlt": 0,
        "jlt_no_authors": 0,
        "mw_letters_no_authors": 0,
        "kept": 0,
    }
    per_type = {"journal": 0, "conference": 0}

    for f in files:
        try:
            df = pd.read_excel(f)
        except Exception as e:
            if strict:
                raise ValueError(f'Cannot read metadata file: {f}') from e
            print(f"    [!] {os.path.basename(f)} 읽기 실패: {e}")
            continue
        cols = list(df.columns)
        colmap = {c: c for c in cols}
        if strict and (df.empty or not {'Title', 'URL'}.issubset(cols)
                       or not ({'Journal', 'Conference'} & set(cols))):
            raise ValueError(f'Empty or malformed metadata file: {f}')

        has_conf = "Conference" in cols
        source_type = "conference" if has_conf else "journal"

        for _, r in df.iterrows():
            stats["read"] += 1
            source_name = clean(pick(colmap, r, "Conference", "Journal"))
            url = clean(pick(colmap, r, "URL"))
            doi = clean(pick(colmap, r, "DOI"))
            if source_name == "JLT":
                key = jlt_doi_key(doi)
                kind = "optica" if key else None
                if not key:
                    stats["legacy_jlt"] += 1
                    continue
            else:
                key, kind = extract_key(url or "")
            if not key:
                stats["no_key"] += 1
                continue

            title = clean(pick(colmap, r, "Title"))
            if not title:
                stats["no_title"] += 1
                continue

            authors = clean(pick(colmap, r, "Authors"))
            if source_name == "JLT" and not authors:
                stats["jlt_no_authors"] += 1
                continue
            if source_name in {"MWCL", "MWTL", "SSC-M"} and not authors:
                stats["mw_letters_no_authors"] += 1
                continue
            if kind == "nature" and not authors:
                stats["nature_no_authors"] += 1
                continue
            year = norm_year(pick(colmap, r, "Year"))
            source_name = normalize_publication_source(source_name, year)
            # issue: 저널=Issue, 학회=Page, Optica=Category
            issue = clean(pick(colmap, r, "Issue", "Category", "Page"))
            if issue and "Page" in colmap and "Issue" not in colmap and "Category" not in colmap:
                issue = f"p.{issue}"

            # pdf 존재 확인: kind 에 따라 ieee-pdf/optica-pdf/nature-pdf 중 결정
            pdf_dir = PDF_DIR_BY_KIND.get(kind, OPTICA_PDF_DIR)
            pdf_path = os.path.join(pdf_dir, f"{key}.pdf")
            pdf_ok = os.path.isfile(pdf_path)
            pdf_local = os.path.relpath(pdf_path, ROOT).replace("\\", "/") if pdf_ok else None

            rec = (
                key, title, authors, year, source_name, source_type, kind,
                issue, url, pdf_local, 1 if pdf_ok else 0, doi,
            )
            # 나중 파일이 최신일 가능성이 높으므로 덮어쓰기(마지막 우선)
            records[key] = rec
            per_type[source_type] = per_type.get(source_type, 0)

    stats["kept"] = len(records)
    # per_type 은 dedup 후 다시 계산
    per_type = {"journal": 0, "conference": 0}
    for rec in records.values():
        per_type[rec[5]] = per_type.get(rec[5], 0) + 1

    print(f"[*] 읽은 행: {stats['read']:,} | 제목없음 skip: {stats['no_title']:,} | "
          f"키없음 skip: {stats['no_key']:,} | Nature 저자없음 skip: {stats['nature_no_authors']:,} | "
          f"JLT 구키 skip: {stats['legacy_jlt']:,} | JLT 저자없음 skip: {stats['jlt_no_authors']:,} | "
          f"MWCL/MWTL/SSC-M 저자없음 skip: {stats['mw_letters_no_authors']:,}")
    print(f"[*] dedup 후 고유 논문: {stats['kept']:,} "
          f"(journal={per_type['journal']:,}, conference={per_type['conference']:,})")
    pdfs = sum(1 for rec in records.values() if rec[10])
    print(f"[*] PDF 보유(pdf_available=1): {pdfs:,}")
    if strict and not records:
        raise ValueError('No importable metadata records')
    return list(records.values())


UPSERT = """
INSERT INTO papers
    (article_number, title, authors, year, source_name, source_type, source_system,
     issue, url, pdf_local_path, pdf_available, doi)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    title=VALUES(title),
    authors=COALESCE(NULLIF(NULLIF(VALUES(authors), ''), 'N/A'), authors),
    year=VALUES(year),
    source_name=VALUES(source_name),
    source_type=VALUES(source_type),
    source_system=VALUES(source_system),
    issue=VALUES(issue),
    url=VALUES(url),
    pdf_local_path=COALESCE(pdf_local_path, VALUES(pdf_local_path)),
    pdf_available=GREATEST(pdf_available, VALUES(pdf_available)),
    doi=COALESCE(NULLIF(VALUES(doi), ''), doi)
"""


def import_rows(rows, conn):
    """Serialize metadata writes; keep favorites/PDF state and verify keys."""
    with conn.cursor() as cur:
        cur.execute("SELECT GET_LOCK('paper_server_metadata_import', 60)")
        if cur.fetchone()[0] != 1:
            raise RuntimeError('Another metadata import is still running')
        try:
            for offset in range(0, len(rows), 2000):
                cur.executemany(UPSERT, rows[offset:offset + 2000])
            for offset in range(0, len(rows), 1000):
                keys = [row[0] for row in rows[offset:offset + 1000]]
                placeholders = ','.join(['%s'] * len(keys))
                cur.execute(f'SELECT article_number FROM papers WHERE article_number IN ({placeholders})', keys)
                found = {str(row[0]) for row in cur.fetchall()}
                if set(keys) - found:
                    raise RuntimeError('Imported keys missing before commit')
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.execute("SELECT RELEASE_LOCK('paper_server_metadata_import')")


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--files', nargs='+', help='Only import these explicit workbooks')
    args = parser.parse_args()
    rows = load_rows(args.files, strict=bool(args.files))
    if not rows:
        print("[!] 넣을 데이터가 없습니다.")
        return

    conn = pymysql.connect(**DB)
    try:
        rows = reconcile_doi_keys(rows, conn)
        import_rows(rows, conn)
        print()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM papers")
            n = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM papers WHERE pdf_available=1")
            npdf = cur.fetchone()[0]
            cur.execute("SELECT source_type, COUNT(*) FROM papers GROUP BY source_type")
            by_type = cur.fetchall()
        print(f"[OK] DB 총 논문 수: {n:,} | PDF 보유: {npdf:,}")
        print(f"[OK] 유형별: {dict(by_type)}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
