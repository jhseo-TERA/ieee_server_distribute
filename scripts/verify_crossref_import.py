"""Verify exact files from completed metadata runs against current MySQL keys."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pymysql
from scripts.import_excel_to_db import DB, load_rows, reconcile_doi_keys
from scripts.run_metadata_update import REPORT_DIR, write_json


def fetch_existing(conn, keys):
    found = set()
    values = sorted(keys)
    with conn.cursor() as cur:
        for offset in range(0, len(values), 1000):
            chunk = values[offset:offset + 1000]
            placeholders = ','.join(['%s'] * len(chunk))
            cur.execute(f'SELECT article_number FROM papers WHERE article_number IN ({placeholders})', chunk)
            found.update(str(row[0]) for row in cur.fetchall())
    return found


def verify_report(conn, report_path):
    report = json.loads(Path(report_path).read_text(encoding='utf-8'))
    if report.get('status') != 'completed':
        raise ValueError(f'Run is not completed: {report_path}')
    results = []
    for item in report['sources']:
        if item['status'] == 'outside_publication_range':
            continue
        if item['status'] != 'validated':
            raise ValueError(f'Unvalidated source in completed run: {item["name"]}')
        path = Path(item['file'])
        if hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
            raise ValueError(f'Workbook changed after validation: {path}')
        records = load_rows([path], strict=True)
        records = reconcile_doi_keys(records, conn)
        keys = {row[0] for row in records}
        missing = keys - fetch_existing(conn, keys)
        result = {'source': item['name'], 'keys': len(keys), 'missing': len(missing),
                  'missing_sample': sorted(missing)[:10]}
        results.append(result)
        print(f'{item["name"]}: keys={len(keys)}, missing={len(missing)}')
    backup_path = report.get('before_import_backup')
    if backup_path:
        before = json.loads(Path(backup_path).read_text(encoding='utf-8'))['rows']
        mismatches = []
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            for offset in range(0, len(before), 1000):
                chunk = before[offset:offset + 1000]
                keys = [row['article_number'] for row in chunk]
                placeholders = ','.join(['%s'] * len(keys))
                cur.execute(f'SELECT article_number,is_favorite,pdf_available,pdf_local_path '
                            f'FROM papers WHERE article_number IN ({placeholders})', keys)
                after = {row['article_number']: row for row in cur.fetchall()}
                for old in chunk:
                    new = after.get(old['article_number'])
                    if (not new or new['is_favorite'] != old['is_favorite']
                            or (old['pdf_available'] and not new['pdf_available'])
                            or (old['pdf_local_path'] and old['pdf_local_path'] != new['pdf_local_path'])):
                        mismatches.append(old['article_number'])
        if mismatches:
            raise ValueError(f'User/PDF state differs from before-import backup: {mismatches[:10]}')
        print(f'Preserved existing favorite/PDF state: {len(before)} records')
    return results


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pipeline', choices=['ieee', 'nature', 'optica'])
    parser.add_argument('--report', type=Path, help='Verify a specific completed run')
    args = parser.parse_args()
    reports = [args.report] if args.report else [
        REPORT_DIR / f'latest_{name}.json'
        for name in ([args.pipeline] if args.pipeline else ['ieee', 'nature', 'optica'])]
    results, errors = [], []
    with pymysql.connect(**DB) as conn:
        for path in reports:
            try:
                results.extend(verify_report(conn, path))
            except Exception as exc:
                errors.append(f'{path.name}: {type(exc).__name__}: {exc}')
    failed = bool(errors) or any(result['missing'] for result in results)
    output = ROOT / 'logs' / 'metadata_updates' / 'verification' / f'{datetime.now():%Y%m%d_%H%M%S}.json'
    write_json(output, {'status': 'failed' if failed else 'passed', 'sources': results, 'errors': errors})
    print(f'[VERIFICATION] {"failed" if failed else "passed"}: {output}')
    for error in errors:
        print(error)
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
