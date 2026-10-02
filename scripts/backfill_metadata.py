"""Resume historical IEEE coverage in isolated source/year units.

Uses a separate report namespace from weekly updates. A validated collection
is cached and hashed, then imported without recollection. Existing papers are
never overwritten; conflicts remain in the unit's changes.json for review.
"""
import argparse
from datetime import datetime
import gzip
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.metadata_enrichment import import_new_records, sha256, write_json
from scripts.metadata_sources import source
from scripts.run_metadata_update import collect_source, fingerprint

RANGES = {
    'TCAS-I': (2006, 2024), 'JSSC': (2006, 2024), 'TCAS-II': (2006, 2024),
    'ISSCC': (2006, 2024), 'VLSI-Circuits': (2006, 2021), 'VLSI-Tech': (2006, 2024),
    'ESSCIRC': (2006, 2023), 'ESSERC': (2024, 2024), 'RFIC': (2006, 2024),
    'ISCAS': (2006, 2024), 'ASSCC': (2006, 2024), 'MWSCAS': (2006, 2024),
    'APCCAS': (2006, 2024), 'NEWCAS': (2006, 2024), 'BCICTS': (2018, 2024),
    'TMTT': (2006, 2024), 'MWCL': (2006, 2022),
}


class EvidenceClient:
    def __init__(self, client, directory):
        self.client, self.directory = client, Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.requests = []

    def get_message(self, endpoint, params):
        message = self.client.get_message(endpoint, params)
        path = self.directory / f'{len(self.requests):04d}.json.gz'
        with gzip.open(path, 'wt', encoding='utf-8') as handle:
            json.dump({'endpoint': endpoint, 'params': params, 'message': message}, handle, ensure_ascii=False)
        self.requests.append({'endpoint': endpoint, 'params': params,
                              'total': message.get('total-results'),
                              'retrieved': len(message.get('items', [])),
                              'file': path.name, 'sha256': sha256(path)})
        write_json(self.directory / 'index.json', self.requests)
        return message


def convert_rows(rows, config, conflicts=None):
    from scripts.import_excel_to_db import extract_key
    converted = {}
    ambiguous = set()
    for row in rows:
        key, system = extract_key(row['URL'])
        name = row.get('Journal') or row.get('Conference')
        if not key or system != 'ieee' or name != config['name']:
            raise ValueError('Collected source or document identity mismatch')
        candidate = dict(article_number=key, title=row['Title'], authors=row['Authors'],
                         year=str(row['Year']), source_name=name, source_type=config['type'],
                         source_system='ieee', issue=row.get('Issue') or (f"p.{row['Page']}" if row.get('Page') else None),
                         url=row['URL'], doi=row['DOI'].lower())
        if key in ambiguous:
            if conflicts is not None:
                conflicts.append({'article_number': key, 'candidate': candidate, 'reason': 'conflicting_document_metadata'})
            continue
        if key in converted and converted[key] != candidate:
            if conflicts is None:
                raise ValueError('Conflicting collected metadata for one document')
            conflicts.append({'article_number': key, 'candidates': [converted.pop(key), candidate],
                              'reason': 'conflicting_document_metadata'})
            ambiguous.add(key)
            continue
        converted[key] = candidate
    return list(converted.values())


def main(argv=None):
    import pymysql
    from scripts.import_excel_to_db import DB
    from scripts.fetch_ieee_crossref import CrossrefClient
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sources', default=','.join(RANGES))
    parser.add_argument('--years', help='Optional comma-separated years within the approved range')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--retry-unresolved', action='store_true')
    parser.add_argument('--revalidate', action='store_true', help='Recollect selected units with the current rules; retain prior application history')
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    names = args.sources.split(',')
    if set(names) - set(RANGES):
        parser.error('Source is outside the reviewed historical scope')
    years = {int(y) for y in args.years.split(',')} if args.years else None
    args.output.mkdir(parents=True, exist_ok=True)
    client = CrossrefClient()
    summary = {'started_at': datetime.now().isoformat(), 'apply': args.apply, 'units': []}
    with pymysql.connect(**DB) as conn:
        for name in names:
            low, high = RANGES[name]
            order = list(range(high, low - 1, -1))
            if name == 'TCAS-I':
                order.remove(2011)
                order.insert(0, 2011)
            for year in order:
                if years and year not in years:
                    continue
                directory = args.output / f'{name}_{year}'
                path = directory / 'unit.json'
                config = source(name)
                unit = {'source': name, 'year': year, 'config_hash': fingerprint(config)}
                if path.exists():
                    old = json.loads(path.read_text(encoding='utf-8'))
                    retrying = args.retry_unresolved and old.get('status') in (
                        'unresolved', 'completed_with_gaps', 'completed_with_conflicts')
                    if args.revalidate or retrying:
                        write_json(directory / 'history' / (datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.json'), old)
                    if args.revalidate:
                        old['status'] = 'requires_revalidation'
                    if old.get('status') in ('completed', 'confirmed_absence'):
                        summary['units'].append(old)
                        continue
                    if old.get('status') in ('unresolved', 'completed_with_gaps', 'completed_with_conflicts') and not args.retry_unresolved:
                        summary['units'].append(old)
                        continue
                    unit = old
                try:
                    data = directory / 'validated_candidates.json'
                    if unit.get('status') in ('validated', 'completed_with_conflicts') and data.exists():
                        if sha256(data) != unit['candidate_sha256'] or unit['config_hash'] != fingerprint(config):
                            raise ValueError('Validated collection/config hash changed')
                        records = json.loads(data.read_text(encoding='utf-8'))
                    else:
                        wrapped = EvidenceClient(client, directory / ('evidence_' + datetime.now().strftime('%H%M%S_%f')))
                        raw, editions = collect_source(config, year, year, wrapped)
                        collector_path = ROOT / 'scripts' / ('fetch_ieee_crossref.py' if config['type'] == 'journal' else 'fetch_ieee_conference_crossref.py')
                        unit.update(config_hash=fingerprint(config), collector_sha256=sha256(collector_path),
                                    editions=editions, requests=wrapped.requests)
                        collection_conflicts = []
                        records = convert_rows(raw, config, collection_conflicts)
                        unit['collection_conflicts'] = len(collection_conflicts)
                        if collection_conflicts:
                            write_json(directory / 'collection_conflicts.json', collection_conflicts)
                        if not records:
                            if editions and all(e.get('status') == 'confirmed_absence' for e in editions):
                                unit['status'] = 'confirmed_absence'
                                unit.pop('error', None)
                                unit.pop('failure_evidence', None)
                                write_json(path, unit)
                                summary['units'].append(unit)
                                continue
                            raise ValueError('zero_records: historical coverage unresolved')
                        with conn.cursor() as cur:
                            cur.execute('SELECT article_number,doi FROM papers WHERE source_name=%s AND year=%s', (name, str(year)))
                            anchors = [{'article_number': str(key), 'doi': doi} for key, doi in cur.fetchall()]
                        conn.commit()
                        keys = {r['article_number'] for r in records}
                        dois = {r['doi'].lower() for r in records}
                        unit['anchor_count'] = len(anchors)
                        unit['missing_existing_anchors'] = [a for a in anchors if a['article_number'] not in keys and (a['doi'] or '').lower() not in dois]
                        write_json(data, records)
                        unit.update(status='validated', candidate_count=len(records), candidate_sha256=sha256(data))
                    # Old failures remain in history, not in the successful
                    # current collection or the cached collection being retried.
                    unit.pop('error', None)
                    unit.pop('failure_evidence', None)
                    write_json(path, unit)
                    if args.apply:
                        application = directory / ('apply_' + datetime.now().strftime('%H%M%S_%f'))
                        result = import_new_records(conn, records, application)
                        status = ('completed_with_conflicts' if result['held'] or unit.get('collection_conflicts') else
                                  'completed_with_gaps' if unit.get('missing_existing_anchors') else 'completed')
                        previous_inserted = unit.get('inserted', 0)
                        unit.setdefault('applications', []).append({'path': str(application), **result})
                        unit.update(status=status, **result, application=str(application))
                        unit['inserted'] += previous_inserted
                    print(json.dumps({k: unit.get(k) for k in ('source','year','status','candidate_count','inserted','existing','held')}, ensure_ascii=False), flush=True)
                except Exception as exc:
                    conn.rollback()
                    unit.update(status='unresolved', error=f'{type(exc).__name__}: {exc}')
                    unit.pop('failure_evidence', None)
                    if hasattr(exc, 'evidence'):
                        unit['failure_evidence'] = exc.evidence
                    print(json.dumps({'source': name, 'year': year, 'status': 'unresolved', 'error': unit['error']}, ensure_ascii=False), flush=True)
                write_json(path, unit)
                summary['units'].append(unit)
                write_json(args.output / 'summary.json', summary)
    summary['finished_at'] = datetime.now().isoformat()
    summary['inserted'] = sum(x.get('inserted', 0) for x in summary['units'])
    summary['unresolved'] = sum(x.get('status') in ('unresolved', 'completed_with_gaps', 'completed_with_conflicts') for x in summary['units'])
    write_json(args.output / 'summary.json', summary)
    return 1 if summary['unresolved'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
