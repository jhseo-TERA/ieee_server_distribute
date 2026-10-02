# -*- coding: utf-8 -*-
r"""Merge legacy JLT keys into stable Crossref DOI keys.

Run after importing a full JLT Crossref workbook. A complete Excel backup of
all legacy JLT database rows is written before any deletion. By default this
script is a dry run; pass ``--apply`` to merge favorite/PDF state and delete
only rows that were mapped safely or authorless front matter with no state.
"""
import argparse
from datetime import datetime
import glob
import os
import re
import shutil
import sys
import unicodedata

import pandas as pd
import pymysql
from dotenv import load_dotenv

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
META_DIR = os.path.join(ROOT, "py_01_data", "00_metadata")
REPORT_DIR = os.path.join(ROOT, "py_02_reports")
load_dotenv(os.path.join(ROOT, ".env"))

DB = dict(
    host=os.getenv("DB_HOST", "127.0.0.1"),
    port=int(os.getenv("DB_PORT", "3306")),
    user=os.getenv("DB_USER", "root"),
    password=os.getenv("DB_PASSWORD", ""),
    database=os.getenv("DB_NAME", "ieee_repo"),
    charset="utf8mb4",
)
URI_RE = re.compile(r"[?&]uri=([\w.-]+)", re.IGNORECASE)


def normalize_title(value):
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^a-z0-9]+", "", text)


def jlt_doi_key(doi):
    value = str(doi or "").strip().lower()
    if not re.match(r"^10\.1109/(?:jlt\.|50\.)", value):
        return None
    return ("jlt-doi-" + re.sub(r"[^a-z0-9]+", "-", value).strip("-"))[:80]


def latest_jlt_workbook():
    for path in sorted(
        glob.glob(os.path.join(META_DIR, "Optica_Crossref_full_*.xlsx")), reverse=True
    ):
        frame = pd.read_excel(path)
        if {"Journal", "DOI", "IEEEArticleNumber"}.issubset(frame.columns):
            jlt = frame[frame["Journal"].eq("JLT")].copy()
            if len(jlt) and jlt["Authors"].notna().all():
                return path, jlt
    raise RuntimeError("저자가 있는 최신 JLT 전체 Crossref 파일을 찾지 못했습니다.")


def build_maps(frame):
    by_uri = {}
    by_ieee = {}
    by_title_candidates = {}
    valid_keys = set()

    for _, row in frame.iterrows():
        target = jlt_doi_key(row.get("DOI"))
        if not target:
            continue
        valid_keys.add(target)
        match = URI_RE.search(str(row.get("URL") or ""))
        if match:
            by_uri[match.group(1).lower()] = target
        ieee_number = row.get("IEEEArticleNumber")
        if pd.notna(ieee_number):
            by_ieee[re.sub(r"\.0$", "", str(ieee_number))] = target
        title_key = normalize_title(row.get("Title"))
        if title_key:
            by_title_candidates.setdefault(title_key, set()).add(target)

    by_title = {
        title: next(iter(targets))
        for title, targets in by_title_candidates.items()
        if len(targets) == 1
    }
    return by_uri, by_ieee, by_title, valid_keys


def main():
    parser = argparse.ArgumentParser(description="JLT 구키를 Crossref DOI 키로 병합")
    parser.add_argument("--apply", action="store_true", help="검증된 병합과 삭제를 실제 적용")
    args = parser.parse_args()

    workbook, frame = latest_jlt_workbook()
    by_uri, by_ieee, by_title, valid_keys = build_maps(frame)
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM papers WHERE source_name=%s "
                "AND article_number NOT LIKE %s ORDER BY id",
                ("JLT", "jlt-doi-%"),
            )
            legacy = cur.fetchall()
            cur.execute(
                "SELECT article_number FROM papers WHERE source_name=%s "
                "AND article_number LIKE %s",
                ("JLT", "jlt-doi-%"),
            )
            present_targets = {row["article_number"] for row in cur.fetchall()}

        missing_targets = valid_keys - present_targets
        if missing_targets:
            raise RuntimeError(
                f"Crossref DOI 대상 {len(missing_targets):,}건이 아직 DB에 없습니다. 먼저 import를 실행하세요."
            )

        mapped = []
        drop_front_matter = []
        retained = []
        for row in legacy:
            key = str(row["article_number"] or "")
            target = None
            if row["source_system"] == "optica":
                target = by_uri.get(key.lower())
            elif row["source_system"] == "ieee":
                target = by_ieee.get(key)
            if not target:
                target = by_title.get(normalize_title(row["title"]))

            if target:
                mapped.append((row, target))
            elif not row.get("authors") and not row.get("is_favorite") and not row.get("pdf_available"):
                drop_front_matter.append(row)
            else:
                retained.append(row)

        print(f"[*] Crossref 파일: {os.path.basename(workbook)} ({len(frame):,}건)")
        print(f"[*] JLT 구키: {len(legacy):,}건")
        print(f"    병합 가능: {len(mapped):,}건")
        print(f"    상태 없는 비논문 삭제 대상: {len(drop_front_matter):,}건")
        print(f"    매핑 불가 보존: {len(retained):,}건")
        print(
            f"    이전할 즐겨찾기: {sum(bool(row.get('is_favorite')) for row, _ in mapped):,} | "
            f"PDF 상태: {sum(bool(row.get('pdf_available')) for row, _ in mapped):,}"
        )

        if retained:
            print("    [RETAINED] 자동 매핑하지 않은 기존 논문:")
            for row in retained:
                print(
                    f"      - {row.get('article_number')} | {row.get('year')} | "
                    f"{row.get('title')}"
                )

        if not args.apply:
            print("[DRY-RUN] 변경하지 않았습니다. 적용하려면 --apply를 사용하세요.")
            return

        os.makedirs(REPORT_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(REPORT_DIR, f"JLT_legacy_backup_{timestamp}.xlsx")
        pd.DataFrame(legacy).to_excel(backup_path, index=False)
        print(f"[*] 삭제 전 백업: {backup_path}")

        copied_pdfs = 0
        with conn.cursor() as cur:
            for row, target in mapped:
                migrated_pdf_path = row.get("pdf_local_path")
                if row.get("pdf_available"):
                    source_path = os.path.join(ROOT, str(migrated_pdf_path or ""))
                    migrated_pdf_path = f"optica-pdf/{target}.pdf"
                    target_path = os.path.join(ROOT, migrated_pdf_path)
                    if not os.path.isfile(source_path) and not os.path.isfile(target_path):
                        raise RuntimeError(f"PDF 파일을 찾을 수 없습니다: {row.get('article_number')}")
                    if not os.path.isfile(target_path):
                        os.makedirs(os.path.dirname(target_path), exist_ok=True)
                        shutil.copy2(source_path, target_path)
                        copied_pdfs += 1
                if row.get("is_favorite") or row.get("pdf_available"):
                    cur.execute(
                        "UPDATE papers SET "
                        "is_favorite=GREATEST(COALESCE(is_favorite,0),%s), "
                        "pdf_available=GREATEST(COALESCE(pdf_available,0),%s), "
                        "pdf_local_path=CASE WHEN %s=1 AND %s IS NOT NULL "
                        "THEN %s ELSE pdf_local_path END "
                        "WHERE article_number=%s AND source_name=%s",
                        (
                            int(bool(row.get("is_favorite"))),
                            int(bool(row.get("pdf_available"))),
                            int(bool(row.get("pdf_available"))),
                            migrated_pdf_path,
                            migrated_pdf_path,
                            target,
                            "JLT",
                        ),
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError(f"상태 이전 대상이 없습니다: {target}")

            delete_ids = [row["id"] for row, _ in mapped]
            delete_ids.extend(row["id"] for row in drop_front_matter)
            for offset in range(0, len(delete_ids), 1000):
                chunk = delete_ids[offset : offset + 1000]
                placeholders = ",".join(["%s"] * len(chunk))
                cur.execute(f"DELETE FROM papers WHERE id IN ({placeholders})", chunk)
        conn.commit()
        print(f"[*] DOI 키 파일명으로 복사한 PDF: {copied_pdfs:,}건")
        print(f"[완료] 구키/비논문 {len(delete_ids):,}건 제거, 미매핑 {len(retained):,}건 보존")
        print(f"[복구용 백업] {backup_path}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
