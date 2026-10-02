"""Discover conference editions, then paginate exact Crossref containers.

Relevance search is used ONLY to discover titles. It is never used as a
truncated list of papers. Discovered editions use exact container filters
where available, and DOI + title + year are checked independently.
"""
from collections import Counter
from datetime import datetime
import re

from scripts.fetch_ieee_crossref import clean_title, extract_ieee_url, format_authors
from scripts.crossref_pagination import PageAudit

FIELDS = ('DOI,title,author,page,container-title,ISBN,resource,published-print,'
          'published-online,published,issued,created')
DISCOVERY_PAGE_SIZE = 1000
MAX_EDITION_QUERY_RESULTS = 10000


class UnresolvedEditionError(RuntimeError):
    """A historical empty result needs evidence, not a successful zero count."""

    def __init__(self, message, evidence):
        super().__init__(message)
        self.evidence = evidence


def title_matches(title, config, year):
    # A few official deposits omit the year (NEWCAS 2012). Permit only an
    # exact, evidenced title registered for that edition; DOI/year checks
    # remain mandatory everywhere this helper is used for collection.
    known_undated_title = title in config.get('edition_titles_without_year', {}).get(str(year), [])
    return bool(re.search(config['title_pattern'], title, re.IGNORECASE)
                and (re.search(rf'\b{year}\b', title) or known_undated_title))


def doi_matches(doi, config, year):
    prefixes = '|'.join(re.escape(value) for value in config.get('doi_prefixes', ['10.1109']))
    patterns = [config['doi_pattern'], *map(re.escape, config.get('doi_aliases_by_year', {}).get(str(year), []))]
    doi_pattern = '|'.join(patterns)
    return bool(re.match(rf"^(?:{prefixes})/(?:{doi_pattern})\d*\.{year}\.",
                         doi or '', re.IGNORECASE))


def item_to_row(item, config, year):
    if not doi_matches(item.get('DOI'), config, year):
        return None
    if not any(title_matches(title, config, year) for title in item.get('container-title', [])):
        return None
    title, authors, url = clean_title(item), format_authors(item.get('author')), extract_ieee_url(item)
    if not title or not authors or not url:
        return None
    return {'Conference': config['name'], 'Year': str(year), 'Page': item.get('page'),
            'Title': title, 'Authors': authors, 'URL': url, 'DOI': item['DOI']}


def matching_titles(items, config, year):
    return {title for item in items if doi_matches(item.get('DOI'), config, year)
            for title in item.get('container-title', []) if title_matches(title, config, year)}


def edition_isbns(items, config, year):
    result = {}
    for item in items:
        if doi_matches(item.get('DOI'), config, year):
            for title in item.get('container-title', []):
                if title_matches(title, config, year):
                    for value in item.get('ISBN', []):
                        isbn = re.sub(r'[ -]', '', str(value)).upper()
                        if not re.fullmatch(r'(?:\d{9}[\dX]|\d{13})', isbn):
                            continue
                        result.setdefault(title, set()).add(isbn)
                        # Historical IEEE deposits may use either ISBN form.
                        if len(isbn) == 10:
                            base = '978' + isbn[:9]
                            check = -sum(int(d) * (1 if i % 2 == 0 else 3)
                                         for i, d in enumerate(base)) % 10
                            result[title].add(base + str(check))
    return result


def discover_titles(config, year, client, base_filter):
    """Exhaust every bounded title-token/year query before selecting editions.

    Crossref combines different query fields as an intersection, whereas a
    long container query can match any of its words. Registry edition_queries
    therefore contain distinctive tokens that cover each accepted title form.
    No publication-date filter is used: old IEEE records can be undated even
    when their DOI and container both explicitly identify the edition year.
    Anchors supplement discovery; they never short-circuit it.
    """
    queries = list(dict.fromkeys(config.get('edition_queries', [])))
    if not queries or any(not isinstance(query, str) or not query.strip() for query in queries):
        raise ValueError(f'Missing bounded edition_queries: {config["name"]}')
    anchors = config.get('edition_anchors', {}).get(str(year), [])
    titles, isbns, trace = set(), {}, []
    for anchor in anchors:
        if not title_matches(anchor['title'], config, year) or not doi_matches(anchor['doi'], config, year):
            raise ValueError(f'Invalid edition anchor: {config["name"]} {year}')
        titles.add(anchor['title'])
        trace.append({'method': 'verified_anchor', 'title': anchor['title'], 'doi': anchor['doi']})
    # Be defensive for callers of this helper that still pass dated filters.
    edition_filter = ','.join(part for part in base_filter.split(',') if 'pub-date:' not in part)
    for query in queries:
        cursor, audit = '*', PageAudit()
        query_titles, query_dois = set(), set()
        entry = {'method': 'bounded_discovery', 'query': query, 'textual_year': year,
                 'pages': 0, 'scanned': 0, 'complete': False, 'matched_titles': []}
        trace.append(entry)
        while True:
            data = client.get_message('/works', {
                'query.container-title': query, 'query.bibliographic': str(year),
                'filter': edition_filter, 'rows': DISCOVERY_PAGE_SIZE,
                'cursor': cursor, 'select': 'DOI,container-title,ISBN',
            })
            expected = int(data.get('total-results', 0))
            if expected > MAX_EDITION_QUERY_RESULTS:
                raise RuntimeError(f'Conference discovery query is not bounded ({expected} records): '
                                   f'{config["name"]} {year} {query}')
            next_cursor = audit.next_cursor(data, DISCOVERY_PAGE_SIZE)
            query_titles.update(matching_titles(data['items'], config, year))
            query_dois.update(item['DOI'].lower() for item in data['items']
                              if doi_matches(item.get('DOI'), config, year)
                              and any(title_matches(title, config, year)
                                      for title in item.get('container-title', [])))
            for title, values in edition_isbns(data['items'], config, year).items():
                isbns.setdefault(title, set()).update(values)
            entry.update(pages=entry['pages'] + 1, scanned=audit.scanned,
                         total_results=audit.expected, matched_titles=sorted(query_titles),
                         matched_doi_count=len(query_dois), complete=not next_cursor)
            # A first matching container does not establish discovery coverage.
            # Continue through the terminal page to include every title variant.
            if not next_cursor:
                break
            if audit.scanned > MAX_EDITION_QUERY_RESULTS:
                raise RuntimeError(f'Conference discovery exceeded its safety bound: '
                                   f'{config["name"]} {year} {query}')
            cursor = next_cursor
        titles.update(query_titles)
    return sorted(titles), trace, isbns


def fetch_year(config, year, client):
    """Collect one edition using the caller's throttled/retrying Crossref client.

    Unknown historical zero results raise UnresolvedEditionError with a
    serializable .evidence record. Current/future unindexed years are explicit
    pending results, never verified editions. An evidenced registry exception
    may identify a confirmed absence without making a network request.
    """
    exception = config.get('nonpublication_exceptions', {}).get(str(year))
    if exception:
        if not exception.get('reason') or not exception.get('evidence_url'):
            raise ValueError('Nonpublication exceptions require a reason and evidence_url')
        return [], {'year': year, 'status': 'confirmed_absence', 'count': 0,
                    'editions': [], 'exception': exception}
    prefix_filters = ','.join(f'prefix:{value}' for value in config.get('doi_prefixes', ['10.1109']))
    edition_filter = f'{prefix_filters},type:proceedings-article'
    titles, discovery, isbns = discover_titles(config, year, client, edition_filter)
    evidence = {'year': year, 'status': 'discovery_unresolved', 'editions': [],
                'count': 0, 'discovery': discovery}
    if not titles:
        if year >= datetime.now().year:
            evidence['status'] = 'not_yet_indexed'
            return [], evidence
        raise UnresolvedEditionError(f'No verified conference edition: {config["name"]} {year}', evidence)
    rows = {}
    editions = []
    for title in titles:
        # Publication dates may be absent even on legitimate papers (e.g.
        # VLSIC2006/1705374). Restrict by edition identity, never publication
        # dates, once the DOI and container independently establish the year.
        # For comma titles, intersect a distinctive container query with a
        # textual year query. Unlike a publication-date filter, the latter
        # includes the year written in a container on an otherwise undated
        # work. ISBN alone is insufficient: old IEEE works can omit it too.
        if ',' not in title:
            criteria = {'filter': f'{edition_filter},container-title:{title}'}
            method = 'exact_container'
        else:
            query = next((query for query in config['edition_queries']
                          if re.search(rf'\b{re.escape(query)}\b', title, re.IGNORECASE)), None)
            if query is None:
                raise ValueError(f'No bounded edition query covers container: {title}')
            criteria = {'filter': edition_filter,
                        'query.container-title': query,
                        'query.bibliographic': str(year)}
            method = 'bounded_textual_year'
        cursor, scanned, expected, pages = '*', 0, None, 0
        rejected, accepted, candidate_dois = Counter(), 0, set()
        audit = PageAudit()
        while True:
            data = client.get_message('/works', {
                **criteria,
                'rows': 1000, 'cursor': cursor, 'select': FIELDS,
            })
            if expected is None:
                expected = data['total-results']
                if expected == 0:
                    raise RuntimeError(f'Discovered edition returned zero records: {title}')
                if expected > MAX_EDITION_QUERY_RESULTS:
                    raise RuntimeError(f'Conference query is not bounded ({expected} records): {title}')
            items = data['items']
            next_cursor = audit.next_cursor(data, 1000)
            scanned += len(items)
            pages += 1
            for item in items:
                doi = (item.get('DOI') or '').lower()
                candidate_dois.add(doi)
                if title not in item.get('container-title', []):
                    rejected['other_container'] += 1
                elif not doi_matches(doi, config, year):
                    rejected['doi_mismatch'] += 1
                elif not clean_title(item):
                    rejected['missing_title'] += 1
                elif not format_authors(item.get('author')):
                    rejected['missing_authors'] += 1
                elif not extract_ieee_url(item):
                    rejected['missing_ieee_url'] += 1
                elif row := item_to_row(item, config, year):
                    accepted += 1
                    rows[doi] = row
                else:
                    rejected['metadata_mismatch'] += 1
            if not next_cursor:
                if scanned < expected:
                    raise RuntimeError(f'Incomplete Crossref pagination: {scanned}/{expected}: {title}')
                break
            cursor = next_cursor
        required = {anchor['doi'].lower() for anchor in config.get('edition_anchors', {}).get(str(year), [])
                    if anchor['title'] == title}
        if required - candidate_dois:
            raise RuntimeError(f'Conference edition missing configured DOI anchors: {sorted(required - candidate_dois)}')
        editions.append({'title': title, 'retrieval_method': method, 'isbn': sorted(isbns.get(title, [])),
                         'candidate_count': scanned, 'accepted_count': accepted,
                         'excluded_count': sum(rejected.values()), 'excluded_reasons': dict(rejected),
                         'pages': pages, 'pagination_complete': True,
                         'anchor_dois_found': sorted(required), 'candidate_dois': sorted(candidate_dois)})
    evidence.update(editions=editions, count=len(rows))
    if not rows:
        evidence['status'] = 'no_eligible_records'
        raise UnresolvedEditionError(f'No eligible conference papers: {config["name"]} {year}', evidence)
    evidence['status'] = 'verified'
    print(f"[{config['name']} {year}] editions={len(titles)}, papers={len(rows)}", flush=True)
    return list(rows.values()), evidence
