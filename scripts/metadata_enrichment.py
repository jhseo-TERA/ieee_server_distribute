"""Guarded metadata changes with immutable plans and per-batch undo records.

This module never deletes papers or changes identifiers, PDF paths or favorites.
Use an explicit plan produced from publisher evidence; do not pass title-search
guesses. All writers share the regular metadata import advisory lock.
"""
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from html import unescape

import pymysql

LOCK = 'paper_server_metadata_import'
EDITABLE = frozenset({'title', 'authors', 'year', 'source_name', 'source_type',
                      'issue', 'url', 'doi'})
PROTECTED = ('id', 'article_number', 'source_system', 'pdf_local_path',
             'pdf_available', 'is_favorite')
INSERT_FIELDS = ('article_number', 'title', 'authors', 'year', 'source_name',
                 'source_type', 'source_system', 'issue', 'url', 'doi')


def normalize_title(value):
    return ' '.join(unicodedata.normalize('NFKC', unescape(str(value or ''))).casefold().split())


def normalize_doi(value):
    value = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', str(value or '').strip(), flags=re.I).lower()
    if not re.fullmatch(r'10\.\d{4,9}/\S+', value):
        raise ValueError('Invalid DOI syntax')
    return value


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    temporary.replace(path)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


@contextmanager
def metadata_lock(conn):
    conn.commit()
    with conn.cursor() as cur:
        cur.execute('SELECT GET_LOCK(%s, 60)', (LOCK,))
        if cur.fetchone()[0] != 1:
            raise RuntimeError('Another metadata writer is active')
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    finally:
        with conn.cursor() as cur:
            cur.execute('SELECT RELEASE_LOCK(%s)', (LOCK,))


def select_matches(conn, keys, dois=()):
    found = {}
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        for column, values in [('article_number', sorted(set(keys))), ('doi', sorted(set(dois)))]:
            for offset in range(0, len(values), 1000):
                chunk = values[offset:offset + 1000]
                cur.execute(f'SELECT * FROM papers WHERE {column} IN (' + ','.join(['%s'] * len(chunk)) + ') FOR UPDATE', chunk)
                for row in cur.fetchall():
                    found[row['article_number']] = row
    return found


def classify_changes(proposals, current):
    """Pure planner: optimistic old values, unique DOI and field allowlist."""
    by_doi = defaultdict(set)
    for key, row in current.items():
        if row.get('doi'):
            by_doi[str(row['doi']).lower()].add(key)
    proposed_dois = defaultdict(set)
    for p in proposals:
        if p['new'].get('doi'):
            proposed_dois[normalize_doi(p['new']['doi'])].add(p['article_number'])
    accepted, skipped = [], []
    seen = set()
    for p in proposals:
        key = p['article_number']
        reason = None
        if key in seen:
            raise ValueError(f'Duplicate proposal key: {key}')
        seen.add(key)
        if not p.get('evidence'):
            raise ValueError(f'Missing evidence: {key}')
        if not p['new'] or set(p['new']) - EDITABLE:
            raise ValueError(f'Forbidden update fields: {key}')
        row = current.get(key)
        new = dict(p['new'])
        if new.get('doi'):
            new['doi'] = normalize_doi(new['doi'])
        if not row:
            reason = 'missing_row'
        elif all(row.get(k) == v for k, v in new.items()):
            reason = 'already_applied'
        elif p.get('id') is not None and row['id'] != p['id']:
            reason = 'identity_changed'
        elif any(row.get(k) != v for k, v in p.get('expected', {}).items()):
            reason = 'stale_expected_fields'
        elif p.get('blank_doi_only') and str(row.get('doi') or '').strip():
            reason = 'nonblank_doi_preserved'
        elif new.get('doi') and ((by_doi[new['doi']] | proposed_dois[new['doi']]) - {key}):
            reason = 'doi_collision'
        if reason:
            skipped.append({'article_number': key, 'reason': reason})
        else:
            accepted.append({'id': row['id'], 'article_number': key,
                             'old': {k: row.get(k) for k in new}, 'new': new,
                             'protected': {k: row.get(k) for k in PROTECTED},
                             'evidence': p['evidence']})
    return accepted, skipped


def apply_proposals(conn, proposals, output_dir, batch_size=2000):
    """Apply only reviewed field changes; first 200 rows form the pilot batch."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    write_json(output_dir / 'plan.json', proposals)
    summary = {'proposed': len(proposals), 'updated': 0, 'skipped': [], 'batches': []}
    # Detect collisions across batches as well as against existing records.
    ownership = defaultdict(set)
    for p in proposals:
        if p['new'].get('doi'):
            ownership[normalize_doi(p['new']['doi'])].add(p['article_number'])
    collisions = {k for keys in ownership.values() if len(keys) > 1 for k in keys}
    safe = [p for p in proposals if p['article_number'] not in collisions]
    summary['skipped'].extend({'article_number': k, 'reason': 'proposal_doi_collision'} for k in sorted(collisions))
    with metadata_lock(conn):
        offset, batch = 0, 0
        while offset < len(safe):
            size = min(200, batch_size) if batch == 0 else batch_size
            chunk = safe[offset:offset + size]
            current = select_matches(conn, [p['article_number'] for p in chunk],
                                     [normalize_doi(p['new']['doi']) for p in chunk if p['new'].get('doi')])
            accepted, skipped = classify_changes(chunk, current)
            audit = {'status': 'prepared', 'changes': accepted, 'skipped': skipped}
            audit_path = output_dir / f'batch_{batch:05d}.json'
            write_json(audit_path, audit)
            try:
                with conn.cursor() as cur:
                    if accepted and all(set(p['new']) == {'doi'} for p in accepted):
                        cur.execute('CREATE TEMPORARY TABLE IF NOT EXISTS metadata_doi_patch '
                                    '(id BIGINT PRIMARY KEY, article_number VARCHAR(80), doi VARCHAR(255))')
                        cur.execute('DELETE FROM metadata_doi_patch')
                        cur.executemany('INSERT INTO metadata_doi_patch VALUES (%s,%s,%s)',
                                        [(p['id'], p['article_number'], p['new']['doi']) for p in accepted])
                        cur.execute('UPDATE papers p JOIN metadata_doi_patch d ON p.id=d.id '
                                    'AND p.article_number=d.article_number SET p.doi=d.doi')
                        if cur.rowcount != len(accepted):
                            raise RuntimeError('DOI update count mismatch')
                    else:
                        for p in accepted:
                            fields = sorted(p['new'])
                            cur.execute('UPDATE papers SET ' + ','.join(f'{field}=%s' for field in fields)
                                        + ' WHERE id=%s AND article_number=%s',
                                        [p['new'][field] for field in fields] + [p['id'], p['article_number']])
                            if cur.rowcount != 1:
                                raise RuntimeError('Metadata update count mismatch')
                after = select_matches(conn, [p['article_number'] for p in accepted])
                for p in accepted:
                    row = after[p['article_number']]
                    if any(row.get(k) != v for k, v in {**p['new'], **p['protected']}.items()):
                        raise RuntimeError('Post-update metadata/protected-field mismatch')
                conn.commit()
                audit['status'] = 'committed'
            except BaseException:
                conn.rollback()
                audit['status'] = 'rolled_back'
                write_json(audit_path, audit)
                raise
            write_json(audit_path, audit)
            summary['updated'] += len(accepted)
            summary['skipped'].extend(skipped)
            summary['batches'].append({'file': audit_path.name, 'updated': len(accepted)})
            write_json(output_dir / 'summary.json', summary)
            print(json.dumps({'batch': batch, 'updated_total': summary['updated'], 'processed': offset + len(chunk)}, ensure_ascii=False), flush=True)
            offset += len(chunk)
            batch += 1
    write_json(output_dir / 'summary.json', summary)
    return summary


def import_new_records(conn, records, output_dir, allow_missing_authors=False):
    """Insert new verified IEEE keys; preserve all existing metadata verbatim."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    write_json(output_dir / 'candidates.json', records)
    with metadata_lock(conn):
        current = select_matches(conn, [r['article_number'] for r in records], [r['doi'] for r in records])
        by_doi = defaultdict(set)
        for key, r in current.items():
            if r.get('doi'):
                by_doi[r['doi'].lower()].add(key)
        new, existing, held = [], [], []
        candidate_owners = defaultdict(set)
        for record in records:
            candidate_owners[normalize_doi(record['doi'])].add(record['article_number'])
        seen = set()
        for r in records:
            key, doi = r['article_number'], normalize_doi(r['doi'])
            if key in seen:
                raise ValueError('Duplicate collected document key')
            seen.add(key)
            if (r['source_system'] != 'ieee' or not key.isdigit() or not r['title']
                    or (not r['authors'] and not allow_missing_authors)):
                raise ValueError('Invalid IEEE candidate')
            if len(candidate_owners[doi]) != 1:
                held.append({'article_number': key, 'reason': 'candidate_doi_multiple_documents', 'candidate': r})
                continue
            if key in current:
                old = current[key]
                if old['doi'] and old['doi'].lower() != doi:
                    held.append({'article_number': key, 'reason': 'existing_doi_conflict', 'candidate': r})
                elif old['source_name'] != r['source_name']:
                    held.append({'article_number': key, 'reason': 'existing_source_conflict', 'candidate': r})
                else:
                    existing.append(key)
                continue
            if by_doi.get(doi):
                held.append({'article_number': key, 'reason': 'doi_other_document', 'candidate': r})
                continue
            by_doi[doi].add(key)
            new.append(r)
        audit = {'status': 'prepared', 'inserted': [], 'new_records': new, 'existing': existing, 'held': held}
        write_json(output_dir / 'changes.json', audit)
        try:
            with conn.cursor() as cur:
                sql = 'INSERT INTO papers (' + ','.join(INSERT_FIELDS) + ') VALUES (' + ','.join(['%s'] * len(INSERT_FIELDS)) + ')'
                for offset in range(0, len(new), 1000):
                    chunk = new[offset:offset + 1000]
                    cur.executemany(sql, [[r.get(k) for k in INSERT_FIELDS] for r in chunk])
                    if cur.rowcount != len(chunk):
                        raise RuntimeError('Insert count mismatch')
            after = select_matches(conn, [r['article_number'] for r in new])
            if len(after) != len(new):
                raise RuntimeError('Inserted document missing')
            for r in new:
                actual = after[r['article_number']]
                if any(actual.get(k) != r.get(k) for k in INSERT_FIELDS):
                    raise RuntimeError('Inserted metadata mismatch')
                audit['inserted'].append({'id': actual['id'], 'article_number': r['article_number']})
            conn.commit()
        except BaseException:
            conn.rollback()
            audit['status'] = 'rolled_back'
            write_json(output_dir / 'changes.json', audit)
            raise
        audit['status'] = 'committed'
        write_json(output_dir / 'changes.json', audit)
        return {'inserted': len(new), 'existing': len(existing), 'held': len(held)}


def main(argv=None):
    import argparse
    import sys
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from scripts.import_excel_to_db import DB
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    proposals = json.loads(args.plan.read_text(encoding='utf-8'))
    with pymysql.connect(**DB) as conn:
        if args.apply:
            result = apply_proposals(conn, proposals, args.output)
        else:
            with metadata_lock(conn):
                current = select_matches(conn, [p['article_number'] for p in proposals],
                                         [p['new']['doi'] for p in proposals if p['new'].get('doi')])
                accepted, skipped = classify_changes(proposals, current)
                result = {'accepted_count': len(accepted), 'skipped': skipped, 'changes': accepted}
                conn.rollback()
            write_json(args.output / 'preview.json', result)
    print(json.dumps({k:v for k,v in result.items() if k not in ('changes','batches','skipped')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
