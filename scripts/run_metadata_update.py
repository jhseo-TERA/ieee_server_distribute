"""Weekly metadata collection with per-source validation and exact-run import.

Default: collect this and last year, reject incomplete runs, import only this
run's files. --collect-only still checks DB/history coverage but does not write
to MySQL. --self-test is offline and does not connect to MySQL or publishers.
"""
import argparse
from collections import Counter
from datetime import datetime
import getpass
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.metadata_sources import load_registry, sources, select_sources, publication_range
from scripts.metadata_health import safe_text

REPORT_DIR = ROOT / 'logs' / 'metadata_updates'
RUN_DIR = ROOT / 'py_01_data' / '00_metadata' / 'runs'


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    temporary.replace(path)


def fingerprint(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def validate_counts(counts, previous, database, ratio):
    """A zero edition can be legitimate, but an entire zero source never is."""
    if sum(counts.values()) == 0:
        raise ValueError('zero_records: no importable papers for the selected source/window')
    for year, count in counts.items():
        baseline = max(float(previous.get(year, 0)), int(database.get(year, 0)))
        if baseline and count < baseline * ratio:
            raise ValueError(f'count_drop: {year}: {count} < {ratio:.0%} of baseline {baseline:g}')


def historical_counts(name, config_hash, high, low):
    history = []
    for path in sorted(REPORT_DIR.glob('*.json'), reverse=True):
        if path.name.startswith('latest_'):
            continue
        try:
            report = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        # Collection-only/failing runs must not lower the accepted baseline.
        if report.get('status') != 'completed' or report.get('range') != [high, low]:
            continue
        for result in report.get('sources', []):
            if (result['name'] == name and result.get('status') == 'validated'
                    and result.get('config_hash') == config_hash):
                history.append(result['counts'])
        if len(history) == 5:
            break
    return {str(year): statistics.mean(item.get(str(year), 0) for item in history)
            for year in range(low, high + 1)} if history else {}


def collect_source(config, high, low, client):
    rows, evidence = [], []
    collector = config['collector']
    if collector == 'ieee_crossref':
        if config['type'] == 'journal':
            from scripts.fetch_ieee_crossref import fetch_journal
            rows = fetch_journal(config['name'], high, low, client)
        else:
            from scripts.fetch_ieee_conference_crossref import fetch_year
            for year in range(high, low - 1, -1):
                batch, edition = fetch_year(config, year, client)
                rows.extend(batch)
                evidence.append(edition)
    elif collector == 'optica_crossref':
        from scripts.fetch_optica_crossref import fetch_journal
        fetch_journal(config, high, low, client, rows.extend)
    elif collector == 'nature_crossref':
        from scripts.fetch_nature_crossref import fetch_journal, DEFAULT_KEYWORDS
        fetch_journal(config, high, low, client, DEFAULT_KEYWORDS, rows.extend)
    elif collector == 'ofc_crossref':
        from scripts.fetch_ofc_crossref import fetch_year
        # Adapter uses the same throttled/retrying client as the other sources.
        class Response:
            def __init__(self, message):
                self.message, self.status_code, self.headers = message, 200, {}
            def raise_for_status(self):
                pass
            def json(self):
                return {'message': self.message}
        class Session:
            def get(self, url, params, timeout):
                return Response(client.get_message('/works', params))
        for year in range(high, low - 1, -1):
            rows.extend(fetch_year(year, Session()))
    else:
        raise ValueError(f'No automatic collector: {config["name"]}')
    return rows, evidence


def database_counts(conn, name, high, low):
    with conn.cursor() as cur:
        cur.execute('SELECT year, COUNT(*) FROM papers WHERE source_name=%s '
                    'AND year BETWEEN %s AND %s GROUP BY year', (name, str(low), str(high)))
        return {str(year): int(count) for year, count in cur.fetchall()}


def coverage_audit(conn):
    with conn.cursor() as cur:
        cur.execute('SELECT source_name, COUNT(*) FROM papers GROUP BY source_name')
        db = dict(cur.fetchall())
    registered = {item['name'] for item in sources(enabled=None)}
    return {'unregistered_db_sources': {name: count for name, count in db.items() if name not in registered},
            'configured_without_db_rows': sorted(registered - set(db))}


def backup_existing(conn, keys, path):
    """Keep pre-update metadata/user state for the exact affected keys."""
    import pymysql
    existing = []
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        for offset in range(0, len(keys), 1000):
            chunk = keys[offset:offset + 1000]
            placeholders = ','.join(['%s'] * len(chunk))
            cur.execute(f'SELECT * FROM papers WHERE article_number IN ({placeholders})', chunk)
            existing.extend(cur.fetchall())
    write_json(path, {'rows': existing})
    return len(existing)


def run(args):
    import pandas as pd
    import pymysql
    from scripts.fetch_ieee_crossref import CrossrefClient
    from scripts.import_excel_to_db import DB, load_rows, import_rows, reconcile_doi_keys

    selected = select_sources(sources(pipeline=args.pipeline), args.sources)
    high, low = max(args.start_year, args.end_year), min(args.start_year, args.end_year)
    if args.full:
        low = min(item['start_year'] for item in selected)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    run_id = f'{stamp}_{args.pipeline}'
    directory = RUN_DIR / run_id
    report_path = REPORT_DIR / f'{run_id}.json'
    report = {'run_id': run_id, 'started_at': datetime.now().isoformat(),
              'pipeline': args.pipeline, 'range': [high, low], 'status': 'running',
              'pid': os.getpid(), 'parent_pid': os.getppid(), 'user': getpass.getuser(),
              'launcher': os.getenv('METADATA_LAUNCHER', 'direct-cli'),
              'collect_only': args.collect_only, 'sources': []}
    write_json(report_path, report)
    conn = None
    try:
        conn = pymysql.connect(**DB)
        report['coverage'] = coverage_audit(conn)
        # Named lock is released even if this process is killed by Task Scheduler.
        with conn.cursor() as cur:
            cur.execute('SELECT GET_LOCK(%s, 0)', (f'metadata_pipeline_{args.pipeline}',))
            if cur.fetchone()[0] != 1:
                raise RuntimeError('A run of this metadata pipeline is already active')
        directory.mkdir(parents=True, exist_ok=False)
        client = CrossrefClient()
        accepted_rows = []
        for config in selected:
            name = config['name']
            result = {'name': name, 'status': 'running', 'config_hash': fingerprint(config)}
            report['sources'].append(result)
            window = publication_range(config, high, low)
            if not window:
                result.update(status='outside_publication_range', count=0)
                write_json(report_path, report)
                continue
            source_high, source_low = window
            result['range'] = list(window)
            print(f'\n[수집] {name} {source_low}~{source_high}', flush=True)
            try:
                before = database_counts(conn, name, source_high, source_low)
                raw, evidence = collect_source(config, source_high, source_low, client)
                result['raw_count'] = len(raw)
                result['editions'] = evidence
                if not raw:
                    raise ValueError('zero_records: collector returned no papers')
                path = directory / f'{name}.xlsx'
                pd.DataFrame(raw).to_excel(path, index=False)
                # Validate using exactly the keys/filters used by the DB importer.
                records = load_rows([str(path)], strict=True)
                records = reconcile_doi_keys(records, conn)
                if any(row[4] != name or row[5] != config['type'] or row[6] != config['system'] for row in records):
                    raise ValueError('Source name/type/system mismatch')
                observed = Counter(str(row[3]) for row in records)
                counts = {str(year): observed[str(year)] for year in range(source_low, source_high + 1)}
                outside = {year: count for year, count in observed.items() if year not in counts}
                # Crossref can match the online year but return a different print
                # year. Keep those rows and record them; compare like years only.
                previous = historical_counts(name, result['config_hash'], high, low)
                result.update(count=len(records), counts=counts, outside_window_counts=outside,
                              database_before=before, historical_mean=previous, file=str(path),
                              sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                validate_counts(counts, previous, before, load_registry()['minimum_count_ratio'])
                result['status'] = 'validated'
                accepted_rows.extend(records)
            except Exception as exc:
                result.update(status='failed', error=f'{type(exc).__name__}: {safe_text(exc)}')
                print(f'[실패] {name}: {result["error"]}', flush=True)
            write_json(report_path, report)
        failed = [item['name'] for item in report['sources'] if item['status'] == 'failed']
        report['failed_sources'] = failed
        report['success_count'] = sum(item['status'] == 'validated' for item in report['sources'])
        report['failure_count'] = len(failed)
        if failed or not accepted_rows:
            raise RuntimeError(f'Collection incomplete; import blocked. failed_sources={failed}')
        # Defensive de-duplication across source files: conflicting ownership
        # indicates a registry/matcher error and must not silently overwrite.
        unique = {}
        for row in accepted_rows:
            if row[0] in unique and unique[row[0]][4:7] != row[4:7]:
                raise ValueError(f'Conflicting source ownership: {row[0]}')
            unique[row[0]] = row
        report['importable_count'] = len(unique)
        if not args.collect_only:
            conn.commit()  # End the read snapshot before verifying imported keys.
            backup_path = directory / 'before_import.json'
            existing_count = backup_existing(conn, list(unique), backup_path)
            report.update(before_import_backup=str(backup_path), existing_key_count=existing_count,
                          new_key_count=len(unique) - existing_count)
            import_rows(list(unique.values()), conn)
            report['imported_count'] = len(unique)
            report['status'] = 'completed'
        else:
            report['status'] = 'collected'
        return 0
    except Exception as exc:
        report.update(status='failed', error=f'{type(exc).__name__}: {safe_text(exc)}')
        print(f'[실패] {report["error"]}', flush=True)
        return 1
    finally:
        if conn is not None:
            conn.close()
        report['finished_at'] = datetime.now().isoformat()
        write_json(report_path, report)
        # The latest status is consumable by a monitor; detailed reports remain
        # immutable by run id, including failed runs and their evidence.
        write_json(REPORT_DIR / f'latest_{args.pipeline}.json', report)
        print(f'[결과] {report["status"]}: {report_path}', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pipeline', choices=['ieee', 'nature', 'optica'], required=True)
    parser.add_argument('--sources', help='Comma-separated registry names within this pipeline')
    parser.add_argument('--start-year', type=int, default=datetime.now().year)
    parser.add_argument('--end-year', type=int, default=datetime.now().year - 1)
    parser.add_argument('--full', action='store_true')
    parser.add_argument('--collect-only', action='store_true')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args(argv)
    try:
        selected = select_sources(sources(pipeline=args.pipeline), args.sources)
        if not selected or min(args.start_year, args.end_year) < 1900:
            raise ValueError('Empty source selection or invalid year')
    except ValueError as exc:
        parser.error(str(exc))
    if args.self_test:
        # Import every required collector to catch syntax/dependency/config
        # errors before the scheduler needs them; no network or DB side effects.
        from scripts import fetch_ieee_crossref, fetch_ieee_conference_crossref
        from scripts import fetch_optica_crossref, fetch_nature_crossref, fetch_ofc_crossref
        from scripts import import_excel_to_db
        print(f'[SELF-TEST OK] {args.pipeline}: ' + ', '.join(item['name'] for item in selected))
        return 0
    return run(args)


if __name__ == '__main__':
    raise SystemExit(main())
