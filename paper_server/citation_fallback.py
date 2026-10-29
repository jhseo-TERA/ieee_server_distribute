"""Crossref/PDF references and IEEE incoming citations with explicit evidence.

The SQL paper library and publisher PDFs are shared bibliographic sources.
Favorites, uploads, account flags and local file paths never enter snapshots.
"""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import os
from pathlib import Path
import re
import time
import unicodedata
from urllib.parse import quote

from pypdf import PdfReader
from sqlalchemy import text

from paper_server.citation_http import CitationHTTP, DAY, SourceUnavailable
from paper_server.citation_provider import (
    FIELDS, MAX_CANDIDATES, OpenAlexProvider, ProviderError, _normalize_work,
    _optional_count, _plain, normalize_doi,
)


VERSION = 2
ROOT = Path(__file__).resolve().parents[1]
SOURCE_LABELS = {'openalex': 'OpenAlex', 'crossref': 'Crossref', 'pdf': '보유 PDF', 'ieee': 'IEEE'}


def title_key(value):
    value = unicodedata.normalize('NFKD', _plain(value)).casefold().replace('×', 'x')
    return ''.join(character for character in value if character.isalnum())


def valid_doi(value):
    try:
        return normalize_doi(value)
    except ValueError:
        return ''


def record_id(doi, article=''):
    return 'D' + sha256(doi.encode()).hexdigest()[:24] if doi else 'SQL:' + str(article)


def reference_entry(raw, number=None, page=None):
    raw = unicodedata.normalize('NFKC', raw)
    raw = re.sub(r'(?<=/)\s+(?=[\w.])', '', raw)
    raw = re.sub(r'\s+', ' ', raw).strip()[:3000]
    quoted = re.search(r'[“"](.+?)[”"]', raw)
    title = quoted.group(1).strip(' ,.') if quoted else ''
    suffix = raw[quoted.end():] if quoted else raw
    year = re.search(r'\b((?:19|20)\d{2})\b', suffix)
    prefix = raw[:quoted.start()] if quoted else raw
    first_author = re.sub(r'\bet\s+al\.?', '', prefix, flags=re.I).split(',')[0].strip().split()
    doi = re.search(r'10\.\d{4,9}/[^\s<>"”]+', raw, re.I)
    return {'title': title, 'year': int(year.group(1)) if year else None,
            'author': first_author[-1] if first_author else '',
            'doi': valid_doi(doi.group(0).rstrip('.,;') if doi else ''),
            'number': number, 'page': page, 'raw': raw}


def extract_references(path):
    """Keep the numbered References section, not incidental bracketed numbers."""
    reader = PdfReader(path, strict=False)
    if len(reader.pages) > 80:
        return []
    chunks, started = [], False
    for index, page in enumerate(reader.pages, 1):
        content = page.extract_text() or ''
        heading = re.search(r'(?im)^\s*(?:REFERENCES|BIBLIOGRAPHY)\s*$', content)
        if heading:
            started, content = True, content[heading.end():]
        if started:
            chunks.append((index, content[:100000]))
    entries = []
    for page, content in chunks:
        matches = list(re.finditer(r'(?m)^\s*\[(\d{1,3})\]\s+', content))
        for index, match in enumerate(matches):
            stop = matches[index+1].start() if index+1 < len(matches) else len(content)
            entries.append(reference_entry(content[match.end():stop], int(match.group(1)), page))
    # Duplicate labels or a non-contiguous section suggest a parsing error.
    labels = [entry['number'] for entry in entries]
    if not labels or labels != list(range(1, len(labels)+1)):
        return []
    return entries[:80]


def matched(entry, work):
    if not entry.get('title') or title_key(entry['title']) != title_key(work.get('title')):
        return False
    year, candidate_year = entry.get('year'), work.get('year')
    if year and (not candidate_year or abs(year-int(candidate_year)) > 1):
        return False
    author_parts = re.findall(r'[^\W\d_]+', str(entry.get('author') or ''), re.UNICODE)
    surname = title_key(max(author_parts, key=len)) if author_parts else ''
    return bool(surname and surname in title_key(work.get('authors', '')))


def sql_work(row):
    doi = valid_doi(row.get('doi'))
    year = str(row.get('year') or '')
    return {'id': record_id(doi, row['article_number']), 'doi': doi,
            'title': _plain(row.get('title')), 'authors': _plain(row.get('authors')),
            'year': int(year) if year.isdigit() else None,
            'source_name': _plain(row.get('source_name')), 'source_system': row.get('source_system') or 'SQL',
            'metadata_source': 'sql', 'citation_count': _optional_count(row.get('citation_count')),
            'citation_source': row.get('citation_source'), 'citation_updated_at': str(row.get('citation_updated_at') or ''),
            'reference_ids': [], 'related_ids': [], 'abstract': ''}


def crossref_work(raw):
    doi = valid_doi(raw.get('DOI'))
    dates = (raw.get('published') or raw.get('issued') or {}).get('date-parts') or [[]]
    year = dates[0][0] if dates and dates[0] else None
    titles = raw.get('title') or []
    return {'id': record_id(doi), 'doi': doi, 'title': _plain(titles[0]) if titles else '',
            'authors': ', '.join(_plain(str(author.get('given') or '')+' '+str(author.get('family') or ''))
                                 for author in (raw.get('author') or [])[:100]),
            'year': year, 'source_name': _plain((raw.get('container-title') or [''])[0]),
            'source_system': 'Crossref', 'metadata_source': 'crossref',
            'citation_count': _optional_count(raw.get('is-referenced-by-count')),
            'citation_source': 'crossref', 'citation_updated_at':datetime.now(timezone.utc).isoformat(),
            'reference_ids': [], 'related_ids': [], 'abstract': ''}


class CitationLibrary:
    def __init__(self, engine, root=ROOT):
        self.engine, self.root = engine, Path(root).resolve()

    def by_doi(self, doi):
        with self.engine.connect() as connection:
            rows = connection.execute(text('SELECT * FROM papers WHERE doi=:doi LIMIT 2'), {'doi': doi}).mappings().all()
        return dict(rows[0]) if len(rows) == 1 else None

    def by_article(self, article):
        with self.engine.connect() as connection:
            row = connection.execute(text('SELECT * FROM papers WHERE article_number=:article LIMIT 1'), {'article': article}).mappings().first()
        return dict(row) if row else None

    def match(self, entry):
        if entry.get('doi'):
            return self.by_doi(entry['doi'])
        title = entry.get('title')
        if not title or len(title_key(title)) < 15:
            return None
        with self.engine.connect() as connection:
            if self.engine.dialect.name in {'mysql', 'mariadb'}:
                rows = connection.execute(text('SELECT * FROM papers WHERE MATCH(title,authors) '
                    'AGAINST(:query IN NATURAL LANGUAGE MODE)>0 ORDER BY MATCH(title,authors) '
                    'AGAINST(:query IN NATURAL LANGUAGE MODE) DESC LIMIT 12'), {'query': title}).mappings().all()
            else:
                rows = connection.execute(text('SELECT * FROM papers WHERE title=:title COLLATE NOCASE LIMIT 12'), {'title': title}).mappings().all()
        matches = [dict(row) for row in rows if matched(entry, sql_work(dict(row)))]
        return matches[0] if len(matches) == 1 else None

    def pdf(self, row):
        roots = {'ieee': 'ieee-pdf', 'optica': 'optica-pdf', 'nature': 'nature-pdf', 'designcon': 'DesignCon'}
        source = str(row.get('source_system') or '').lower()
        if source not in roots or not row.get('pdf_available'):
            return None
        allowed = (self.root / roots[source]).resolve()
        value = str(row.get('pdf_local_path') or '').replace('\\', '/')
        path = (self.root/value).resolve() if value else (allowed/(str(row['article_number'])+'.pdf')).resolve()
        if not path.is_relative_to(allowed) or path.suffix.lower() != '.pdf' or not path.is_file() or path.stat().st_size > 40_000_000:
            return None
        return path


class SupplementedCitationProvider:
    cache_version = VERSION

    def __init__(self, library, primary=None, http=None):
        self.library = library
        self.primary = primary or OpenAlexProvider()
        self.http = http or CitationHTTP()
        self.ieee_key = os.getenv('IEEE_API_KEY', '').strip()

    def fetch(self, doi):
        try:
            snapshot = self.primary.fetch(doi)
        except ProviderError as original:
            if original.status != 404:
                raise
            row = self.library.by_doi(doi)
            if row is not None:
                seed = sql_work(row)
            else:
                try:
                    seed = crossref_work(self._crossref(doi))
                except SourceUnavailable:
                    raise original from None
                if not seed['doi'] or not seed['title']:
                    raise original
            snapshot = {'seed_id': seed['id'], 'works': [seed], 'meta': {'warnings': [], 'request_count': 0}}
        return self.enrich(snapshot)

    def _crossref(self, doi):
        payload = self.http.get('crossref', 'https://api.crossref.org/works/'+quote(doi, safe=''), ttl=DAY)
        raw = payload.get('message') or {}
        if valid_doi(raw.get('DOI')) != doi:
            raise SourceUnavailable('crossref_identity_mismatch')
        return raw

    def enrich(self, snapshot):
        result = deepcopy(snapshot)
        works = {work['id']: work for work in result['works']}
        seed, meta = works[result['seed_id']], result.setdefault('meta', {})
        before = self.http.counts()
        deadline = time.monotonic()+50
        sources = {'openalex'} if any(work['id'].startswith('W') for work in works.values()) else set()
        notes, raw_crossref = [], None
        seed_row = self.library.by_doi(seed['doi']) if seed.get('doi') else None
        reference_source, entries = 'openalex', []
        if not seed.get('reference_ids'):
            try:
                raw_crossref = self._crossref(seed['doi'])
                sources.add('crossref')
                for index, item in enumerate((raw_crossref.get('reference') or [])[:80], 1):
                    entry = reference_entry(item.get('unstructured') or '', index)
                    entry.update(doi=valid_doi(item.get('DOI')) or entry['doi'],
                                 title=_plain(item.get('article-title')) or entry['title'],
                                 author=_plain(item.get('author')) or entry['author'],
                                 year=_optional_count(item.get('year')) or entry['year'])
                    entries.append(entry)
                if entries:
                    reference_source = 'crossref'
                if entries and not any(entry['doi'] or entry['title'] for entry in entries):
                    entries = []
            except SourceUnavailable:
                notes.append('Crossref 참고문헌을 조회하지 못했습니다.')
            if not entries and seed_row:
                path = self.library.pdf(seed_row)
                if path:
                    try:
                        entries = extract_references(path)
                        if entries:
                            reference_source = 'pdf'
                    except Exception:
                        notes.append('보유 PDF의 참고문헌을 읽지 못했습니다.')
        resolved, unresolved, searches, lookups = [], [], 0, 0
        for entry in entries:
            row = self.library.match(entry)
            candidate = sql_work(row) if row else None
            if candidate is None and time.monotonic() < deadline:
                try:
                    if entry['doi'] and lookups < 8:
                        lookups += 1
                        candidate = crossref_work(self._crossref(entry['doi']))
                    elif entry['title'] and entry['author'] and entry['year'] and searches < 4:
                        searches += 1
                        payload = self.http.get('crossref', 'https://api.crossref.org/works',
                            {'query.bibliographic': ' '.join((entry['title'],entry['author'],str(entry['year']))), 'rows': 2}, ttl=30*DAY)
                        matches = [crossref_work(item) for item in (payload.get('message') or {}).get('items', [])]
                        matches = [item for item in matches if matched(entry, item) and item['doi']]
                        if len(matches) == 1:
                            candidate = matches[0]
                except SourceUnavailable:
                    pass
            if candidate and candidate.get('title') and candidate['id'] != seed['id'] and candidate.get('doi') != seed.get('doi'):
                resolved.append((entry, candidate))
            else:
                unresolved.append({'number': entry['number'], 'title': entry['title'], 'year': entry['year']})
        # One batched exact-DOI lookup improves bibliographic data and preserves
        # OpenAlex IDs. A title search is never itself an edge.
        candidates = {}
        if resolved and time.monotonic() < deadline:
            dois = list(dict.fromkeys(work['doi'] for _, work in resolved if work.get('doi')))[:80]
            if dois:
                try:
                    for work in self.primary.lookup_dois(dois):
                        if work.get('doi') in dois:
                            work['metadata_source'] = 'openalex'
                            if work.get('citation_count') is not None:
                                work.update(citation_source='openalex',citation_updated_at=datetime.now(timezone.utc).isoformat())
                            candidates[work['doi']] = work
                except ProviderError:
                    notes.append('일부 참고문헌의 OpenAlex 상세정보를 보완하지 못했습니다.')
                lookup_count = getattr(getattr(self.primary, '_sessions', None), 'lookup_request_count', 0)
                if type(lookup_count) is int:
                    meta['supplemental_openalex_requests'] = lookup_count

        def add(work):
            existing = next((item for item in works.values() if work.get('doi') and item.get('doi') == work['doi']), None)
            if existing:
                return existing
            if len(works) >= MAX_CANDIDATES:
                return None
            works[work['id']] = work
            return work

        def link(citing, cited, source, entry=None):
            references = citing.setdefault('reference_ids', [])
            if cited['id'] not in references:
                references.append(cited['id'])
                detail = {'target': cited['id'], 'source': source}
                if entry:
                    detail.update(number=entry.get('number'), page=entry.get('page'))
                citing.setdefault('reference_evidence', []).append(detail)
            sources.add(source)

        mapped = 0
        for entry, candidate in resolved:
            work = add(candidates.get(candidate.get('doi')) or candidate)
            if work:
                link(seed, work, reference_source, entry)
                mapped += 1
            else:
                unresolved.append({'number': entry['number'], 'title': entry['title'], 'year': entry['year']})
        if entries:
            meta.update(reference_origin=reference_source, reference_total=len(entries), reference_fetched=mapped,
                        reference_unresolved=len(unresolved), unresolved_references=unresolved,
                        reference_truncated=bool(unresolved))
            if unresolved:
                meta['coverage_truncated'] = True
        else:
            meta.setdefault('reference_origin', 'openalex')
        ieee = {'status': 'not_applicable', 'added': 0, 'types': {}}
        article = str(seed_row.get('article_number') or '') if seed_row else ''
        if self.ieee_key and seed.get('doi', '').startswith('10.1109/') and article.isdigit():
            try:
                payload = self.http.get('ieee', 'https://ieeexploreapi.ieee.org/api/v1/search/articles',
                    {'format': 'json', 'max_records': 25, 'querytext': article}, key=self.ieee_key, ttl=DAY)
                rows = [row for row in payload.get('articles', []) if str(row.get('article_number')) == article
                        and valid_doi(row.get('doi')) == seed['doi']]
                if len(rows) != 1 or _optional_count(rows[0].get('citing_paper_count')) is None:
                    raise SourceUnavailable('ieee_metadata_mismatch')
                ieee.update(status='complete', total=_optional_count(rows[0]['citing_paper_count']))
                sources.add('ieee')
                if ieee['total']:
                    for kind in ('ieee', 'non_ieee'):
                        if time.monotonic() >= deadline:
                            ieee['status'] = 'partial'
                            break
                        try:
                            payload = self.http.get('ieee', 'https://ieeexploreapi.ieee.org/api/v1/search/document/'+article+'/citation',
                                {'format':'json', 'type':kind, 'max_records':60, 'start_record':1}, key=self.ieee_key, ttl=7*DAY)
                            records = payload.get(kind+'_citation') or []
                            total = _optional_count(payload.get(kind+'_citation_count'))
                            if not isinstance(records, list) or str(payload.get('article_number')) != article or total is None:
                                raise SourceUnavailable('ieee_citations_invalid')
                            ieee['types'][kind] = {'total': total, 'fetched': len(records)}
                            if total > len(records):
                                ieee['status'] = 'partial'
                            for raw in records[:60]:
                                links = raw.get('links') or {}
                                doi = valid_doi(links.get('doi'))
                                cited_article = str(links.get('articleNumber') or '')
                                title = _plain(raw.get('title')).strip('"“” ')
                                if not title or not (doi or cited_article.isdigit()):
                                    continue
                                local = self.library.by_doi(doi) if doi else self.library.by_article(cited_article)
                                detail = reference_entry(_plain(raw.get('displayText')))
                                work = sql_work(local) if local else {'id':record_id(doi,cited_article), 'doi':doi,
                                    'title':title, 'authors':'', 'year':detail['year'], 'source_system':'IEEE',
                                    'metadata_source':'ieee', 'reference_ids':[], 'citation_count':None, 'abstract':''}
                                node = add(work)
                                if node and node['id'] != seed['id']:
                                    previously = seed['id'] in node.get('reference_ids', [])
                                    link(node, seed, 'ieee')
                                    ieee['added'] += int(not previously)
                                else:
                                    ieee['status'] = 'partial'
                        except SourceUnavailable as exc:
                            ieee.update(status='partial', reason=exc.reason)
                # Keep counts from different indexes separate, never add them.
                meta['ieee_citation_count'] = ieee['total']
            except SourceUnavailable as exc:
                ieee.update(status='deferred', reason=exc.reason)
            ieee['budget'] = self.http.budget(self.ieee_key)
        elif not self.ieee_key:
            ieee['status'] = 'not_configured'
        after = self.http.counts()
        meta.update(enrichment_version=VERSION, sources=sorted(sources), source_labels=[SOURCE_LABELS[name] for name in sorted(sources)],
                    enriched_at=datetime.now(timezone.utc).isoformat(),
                    ieee=ieee, source_notes=notes,
                    supplemental_requests={name:after.get(name,0)-before.get(name,0) for name in ('crossref','ieee')})
        if sources != {'openalex'}:
            meta['license'] = 'source-specific'
        if ieee['status'] in ('partial', 'deferred'):
            meta['coverage_truncated'] = True
        result['works'] = list(works.values())
        return result
