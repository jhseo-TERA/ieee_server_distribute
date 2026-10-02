"""Restore missing Survey authors/citations from identity-checked public Crossref data.

No PDF/measurement/favorite changes. Existing paper fields are never overwritten.
Survey citation snapshots survive the Repo's non-favorite citation cleanup.
Default is cached-data inspection; --fetch permits HTTP; --apply permits DB writes.
Every run saves before/after values, match decisions and original provider records.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from hashlib import sha256
import argparse
import json
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import parse_qs, quote, urlparse

import requests
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.update_favorite_citations import normalize_doi, normalize_title, title_similarity

CACHE = ROOT / 'outputs' / 'serdes_bibliography' / 'crossref-v2'
LEGACY_CACHE = ROOT / 'outputs' / 'serdes_bibliography' / 'crossref'
GATE, LOCAL = threading.Lock(), threading.local()
NEXT_REQUEST = 0.0
REQUEST_INTERVAL = 1.0


class RateLimited(RuntimeError):
    """Stop and preserve progress when the provider asks us to back off."""


def document_ids(record):
    urls = [record.get('URL'), (record.get('resource') or {}).get('primary', {}).get('URL')]
    urls += [link.get('URL') for link in record.get('link', []) if isinstance(link, dict)]
    found = set()
    for value in urls:
        if not isinstance(value, str):
            continue
        parsed = urlparse(value)
        if not (parsed.hostname or '').endswith('.ieee.org'):
            continue
        found.update(re.findall(r'/(?:document/)?(\d{5,12})(?:\.pdf|/|$)', parsed.path))
        found.update(k for k in parse_qs(parsed.query).get('arnumber', []) if k.isdigit())
    return found


def match_record(paper, record):
    doi, expected = normalize_doi(record.get('DOI')), normalize_doi(paper.get('doi'))
    # Jointly sponsored VLSI/ESSCIRC proceedings may use IEEE's 10.23919 prefix.
    if not doi or not doi.startswith(('10.1109/', '10.23919/')) or (expected and doi != expected):
        return None
    if record.get('type') not in (None, 'journal-article', 'proceedings-article'):
        return None
    if re.search(r'/(?:mm\d+|video|supplement\w*)$', doi):
        return None
    ids = document_ids(record)
    if ids:
        return 'exact_ieee_document' if str(paper['article_number']) in ids else None
    if expected == doi:
        titles = record.get('title') or []
        title = titles[0] if titles else ''
        sequence, jaccard = title_similarity(paper.get('title', ''), title)
        if normalize_title(paper.get('title', '')) == normalize_title(title) or (sequence >= .96 and jaccard >= .9):
            return 'exact_doi_title'
    return None


def parse_record(paper, record):
    method = match_record(paper, record)
    if not method:
        return None
    names = []
    for author in record.get('author') or []:
        name = ' '.join(str(author.get(k) or '').strip() for k in ('given', 'family')).strip()
        name = name or str(author.get('name') or '').strip()
        if name:
            names.append(name)
    count = record.get('is-referenced-by-count')
    # Missing is unknown, not zero. Preserve a genuine zero returned by Crossref.
    count = count if type(count) is int and 0 <= count <= 4294967295 else None
    return {'doi': normalize_doi(record['DOI']), 'authors': '; '.join(names) or None,
            'citation_count': count, 'provider': 'crossref', 'identity_method': method,
            'source_url': 'https://api.crossref.org/works/' + quote(normalize_doi(record['DOI']), safe='')}


def get_json(url, params=None):
    global NEXT_REQUEST
    if not hasattr(LOCAL, 'session'):
        LOCAL.session = requests.Session()
        LOCAL.session.headers['User-Agent'] = 'IEEE-Paper-Server/1.0 (SerDes bibliographic restoration)'
    for attempt in range(3):
        with GATE:
            delay = max(0, NEXT_REQUEST-time.monotonic())
            if delay:
                time.sleep(delay)
            NEXT_REQUEST = time.monotonic()+REQUEST_INTERVAL
        try:
            response = LOCAL.session.get(url, params=params, timeout=(8, 25), allow_redirects=False)
        except requests.RequestException:
            if attempt == 2:
                raise RuntimeError('Crossref connection failed') from None
            time.sleep(2**attempt)
            continue
        if response.status_code == 404:
            return None
        if response.status_code == 429:
            # Stop this run rather than continue pushing against a shared quota.
            raise RateLimited('Crossref rate limit; retry later')
        if response.status_code >= 500 and attempt < 2:
            time.sleep(2**attempt)
            continue
        if response.status_code != 200:
            raise RuntimeError(f'Crossref HTTP {response.status_code}')
        return response.json().get('message')
    return None


def acquire(paper, fetch):
    path = CACHE / (str(paper['article_number']) + '.json')
    if path.exists():
        cached = json.loads(path.read_text(encoding='utf-8'))
        return paper, cached
    # Preserve v1 responses. Only its unmatched searches need the broader v2 query.
    legacy = LEGACY_CACHE / path.name
    if legacy.exists():
        cached = json.loads(legacy.read_text(encoding='utf-8'))
        if cached.get('record') and match_record(paper, cached['record']):
            return paper, cached
        if not fetch:
            return paper, cached
    if not fetch:
        return paper, {'status': 'not_cached'}
    doi = normalize_doi(paper.get('doi'))
    if doi:
        record = get_json('https://api.crossref.org/works/' + quote(doi, safe=''))
        records = [record] if record else []
    else:
        payload = get_json('https://api.crossref.org/works', {'query.bibliographic': paper['title'], 'rows': 5})
        records = (payload or {}).get('items', [])
    matching = [record for record in records if match_record(paper, record)]
    # Multiple distinct DOI matches cannot be silently chosen.
    unique = {normalize_doi(r.get('DOI')): r for r in matching}
    record = next(iter(unique.values())) if len(unique) == 1 else None
    item = {'status': 'matched' if record else 'unmatched', 'fetched_at': datetime.now(timezone.utc).isoformat(),
            'record': record, 'candidates': [{k:r.get(k) for k in ('DOI','title','resource')} for r in records]}
    CACHE.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as handle:
        json.dump(item, handle, ensure_ascii=False, indent=2)
    return paper, item


def apply_record(conn, paper, parsed, fetched_at, record):
    # Recheck both identity and missingness under the write transaction.
    current = conn.execute(text('SELECT authors,doi,citation_count,is_favorite FROM papers WHERE id=:id AND article_number=:article_number FOR UPDATE'), paper).mappings().one()
    if current['doi'] and normalize_doi(current['doi']) != parsed['doi']:
        return {'status': 'identity_changed'}
    values = {**parsed, 'paper_id': paper['id'], 'fetched_at': datetime.fromisoformat(fetched_at).astimezone(timezone.utc).replace(tzinfo=None),
              'record_sha256': sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
    conn.execute(text('''INSERT INTO serdes_paper_bibliography
        (paper_id,doi,authors,citation_count,provider,identity_method,source_url,fetched_at,record_sha256)
        VALUES (:paper_id,:doi,:authors,:citation_count,:provider,:identity_method,:source_url,:fetched_at,:record_sha256)
        ON DUPLICATE KEY UPDATE paper_id=VALUES(paper_id)'''), values)
    updates = {}
    if not str(current['authors'] or '').strip() and parsed['authors']:
        updates['authors'] = parsed['authors']
    if not str(current['doi'] or '').strip():
        updates['doi'] = parsed['doi']
    # Keep the repository's existing favorite-only citation behavior intact.
    if current['is_favorite'] and current['citation_count'] is None and parsed['citation_count'] is not None:
        updates.update(citation_count=parsed['citation_count'], citation_source=parsed['provider'], citation_updated_at=values['fetched_at'])
    if updates:
        conn.execute(text('UPDATE papers SET ' + ','.join(f'{k}=:{k}' for k in updates) + ' WHERE id=:paper_id'), {**updates, 'paper_id': paper['id']})
    return {'status': 'applied', 'fields': sorted(updates), 'before': dict(current), 'after': updates}


def main():
    global REQUEST_INTERVAL
    from web.app import engine, SERDES_LATEST_COMPLETE_RUNS_SQL
    parser = argparse.ArgumentParser()
    parser.add_argument('--fetch', action='store_true')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--interval', type=float, default=1.0, help='Seconds between requests (minimum 0.25; one request at a time)')
    args = parser.parse_args()
    if not .25 <= args.interval <= 60:
        parser.error('--interval must be between 0.25 and 60 seconds')
    REQUEST_INTERVAL = args.interval
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    report_dir = ROOT / 'outputs' / 'serdes_bibliography' / stamp
    report_dir.mkdir(parents=True)
    with engine.connect() as conn:
        papers = [dict(r) for r in conn.execute(text(f'''SELECT DISTINCT p.id,p.article_number,p.title,p.year,p.doi,p.authors,
            p.citation_count,p.citation_source,p.citation_updated_at,p.is_favorite,p.pdf_available
            FROM papers p JOIN serdes_paper_screenings s ON s.paper_id=p.id
             AND s.run_id IN ({SERDES_LATEST_COMPLETE_RUNS_SQL})
            WHERE s.include_in_survey=1 AND p.source_system='ieee'
            ORDER BY CAST(p.year AS UNSIGNED) DESC,p.id DESC''')).mappings()]
    if args.limit > 0:
        papers = papers[:args.limit]
    # Full source rows saved before any schema/data writes.
    (report_dir/'before.json').write_text(json.dumps(papers, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    if args.apply:
        from scripts.serdes_data_pipeline import split_sql_statements
        with engine.begin() as conn:
            for statement in split_sql_statements((ROOT/'scripts/migrations/010_serdes_bibliography.sql').read_text(encoding='utf-8')):
                conn.execute(text(statement))
    stats = {'targets': len(papers), 'matched': 0, 'unmatched': 0, 'errors': 0, 'authors_filled': 0, 'citations_filled': 0, 'dois_filled': 0}
    with (report_dir/'changes.jsonl').open('x', encoding='utf-8') as journal:
        # Crossref's unauthenticated public pool allows only ONE active request.
        # Cache reuse makes interrupted runs resumable without repeated downloads.
        with ThreadPoolExecutor(max_workers=1) as pool:
            futures = {pool.submit(acquire, p, args.fetch): p for p in papers}
            for index, future in enumerate(as_completed(futures), 1):
                paper = futures[future]
                try:
                    paper, cached = future.result()
                    record = cached.get('record')
                    parsed = parse_record(paper, record) if record else None
                    if parsed:
                        stats['matched'] += 1
                        detail = {'status': 'validated', **parsed}
                        if args.apply:
                            with engine.begin() as conn:
                                detail.update(apply_record(conn, paper, parsed, cached['fetched_at'], record))
                            for field, stat in [('authors','authors_filled'),('citation_count','citations_filled'),('doi','dois_filled')]:
                                stats[stat] += field in detail.get('fields', [])
                    else:
                        stats['unmatched'] += 1
                        detail = {'status': cached['status']}
                except Exception as exc:
                    stats['errors'] += 1
                    detail = {'status': 'error', 'error_type': type(exc).__name__}
                    if isinstance(exc, RateLimited):
                        for task in futures:
                            task.cancel()
                        stats['stopped_reason'] = 'provider_rate_limit'
                journal.write(json.dumps({'paper_id':paper['id'], 'article_number':paper['article_number'], **detail},ensure_ascii=False,default=str)+'\n')
                journal.flush()
                if stats.get('stopped_reason'):
                    break
                if index % 50 == 0 or index == len(papers):
                    print(json.dumps({'processed':index, **stats}), flush=True)
    stats['report_dir'] = str(report_dir)
    (report_dir/'summary.json').write_text(json.dumps(stats, indent=2), encoding='utf-8')
    print(json.dumps(stats), flush=True)
    return 1 if stats['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
