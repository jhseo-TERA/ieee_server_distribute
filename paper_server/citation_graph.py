"""Evidence-based, bounded citation maps and a shared public-metadata cache.

Only provider metadata enters the cache. Library membership, PDFs and favorites
are joined by the request thread after retrieval, so no account state is shared.
"""
from collections import Counter, OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from hashlib import sha256
from itertools import combinations
import json
import logging
from math import sqrt
from pathlib import Path
import threading
import time
from uuid import uuid4

from paper_server.citation_provider import OpenAlexProvider, ProviderError, normalize_doi


METHOD = 'bibliographic_coupling_observed_cocitation_v1'
CACHE_TTL = 30 * 24 * 60 * 60
PARTIAL_CACHE_TTL = 5 * 60
EMPTY_CACHE_TTL = 24 * 60 * 60
MAX_WORKS = 180
WORK_FIELDS = ('id', 'doi', 'title', 'authors', 'year', 'source_name', 'source_system',
               'citation_count', 'reference_ids', 'related_ids', 'abstract',
               'references_truncated', 'metadata_source', 'citation_source',
               'citation_updated_at', 'reference_evidence')
COVERAGE_NOTE = ('OpenAlex와 보완 출처에서 제한된 주변 논문을 수집해 계산한 지도입니다. '
                 '공통 참고문헌과 수집 범위 내 동시 인용을 사용하며, 전체 인용망이나 '
                 'Connected Papers의 결과와 같지 않습니다. 계층 탐색은 저장된 직접 '
                 '인용 기록만 사용하며, 추가 조회 없이 기준 논문에서 단계별로 탐색합니다.')
logger = logging.getLogger(__name__)


def _cache_ttl(snapshot):
    if snapshot.get('meta', {}).get('warnings'):
        return PARTIAL_CACHE_TTL
    if snapshot.get('meta', {}).get('ieee', {}).get('status') in ('partial', 'deferred'):
        return 3600
    if snapshot.get('meta', {}).get('reference_unresolved'):
        return EMPTY_CACHE_TTL
    # Newly indexed papers may have no references yet. Revisit these snapshots
    # sooner, including old disk caches created under the 30-day policy.
    return EMPTY_CACHE_TTL if len(snapshot.get('works') or []) <= 1 else CACHE_TTL


def _public_snapshot(snapshot):
    works = [{key: row.get(key) for key in WORK_FIELDS if key in row}
             for row in snapshot['works'][:MAX_WORKS]]
    if not any(row.get('id') == snapshot['seed_id'] for row in works):
        raise ValueError('Seed missing from provider result')
    return {'seed_id': snapshot['seed_id'], 'works': works,
            'fetched_at': snapshot.get('fetched_at'), 'meta': dict(snapshot.get('meta') or {})}


def build_citation_graph(snapshot, limit=30):
    """Use actual reference IDs; titles and provider recommendations add no edges.

    Coupling is reference cosine; co-citation cosine is discounted when only a
    few downloaded citing records support it. Direct citations get a small
    floor/bonus. Every displayed neighbor has evidence connecting it to seed.
    """
    limit = max(1, min(int(limit), 60))
    works = {str(row['id']): row for row in snapshot['works'][:MAX_WORKS] if row.get('id')}
    seed = str(snapshot['seed_id'])
    if seed not in works:
        raise ValueError('Seed missing from provider result')
    refs = {key: set(row.get('reference_ids') or []) - {key} for key, row in works.items()}
    citers = defaultdict(set)
    for citing, cited_ids in refs.items():
        for cited in cited_ids:
            citers[cited].add(citing)

    def evidence(left, right):
        shared = len(refs[left] & refs[right])
        co = len(citers[left] & citers[right])
        direct = left in refs[right] or right in refs[left]
        coupling = shared / sqrt(len(refs[left]) * len(refs[right])) if shared else 0
        cocitation = (co / sqrt(len(citers[left]) * len(citers[right])) * co / (co + 2)
                      if co else 0)
        weight = min(1.0, .65 * coupling + .35 * cocitation + (.12 if direct else 0))
        result = {'shared_references': shared, 'co_citations': co,
                  'direct_citation': direct, 'weight': round(weight, 6)}
        details = [item for citing,cited in ((left,right),(right,left))
                   for item in works[citing].get('reference_evidence', []) if item.get('target') == cited]
        if details:
            result['direct_citation_sources'] = sorted({item['source'] for item in details})
            result['direct_citation_evidence'] = details
        return result

    seed_evidence = {key: evidence(seed, key) for key in works if key != seed}
    ranking = sorted((key for key in seed_evidence if seed_evidence[key]['weight'] > 0),
                     key=lambda key: (-seed_evidence[key]['weight'], key))
    selected = [seed, *ranking[:limit - 1]]

    def node(key):
        row = works[key]
        relation = seed_evidence.get(key, {'shared_references': 0, 'co_citations': 0,
                                          'direct_citation': False, 'weight': 1.0})
        return {'id': key, 'doi': row.get('doi'), 'title': str(row.get('title') or ''),
                'authors': row.get('authors'), 'year': row.get('year'),
                'source_name': row.get('source_name'),
                'source_system': row.get('source_system') or 'OpenAlex',
                'citation_count': row.get('citation_count'), 'reference_count': len(refs[key]),
                'metadata_source': row.get('metadata_source') or ('openalex' if key.startswith('W') else 'sql'),
                'citation_source': (row.get('citation_source') or ('openalex' if key.startswith('W') else None)) if row.get('citation_count') is not None else None,
                'citation_updated_at': (row.get('citation_updated_at') or snapshot.get('fetched_at')) if row.get('citation_count') is not None else None,
                'abstract': str(row.get('abstract') or '')[:12000],
                'abstract_available': bool(row.get('abstract')), 'is_seed': key == seed,
                'similarity': relation['weight'],
                'related_by': {name: relation[name] for name in
                               ('shared_references', 'co_citations', 'direct_citation',
                                'direct_citation_sources', 'direct_citation_evidence') if name in relation}}

    # Retain every seed connection, plus each neighbor's three strongest links.
    # This bounds clutter without drawing a link unsupported by reference data.
    possible = {tuple(sorted((left, right))): evidence(left, right)
                for left, right in combinations(selected, 2)}
    retained = {pair for pair, relation in possible.items() if seed in pair and relation['weight']}
    for key in selected[1:]:
        strongest = sorted((pair for pair, relation in possible.items()
                            if key in pair and relation['weight']),
                           key=lambda pair: (-possible[pair]['weight'], pair))[:3]
        retained.update(strongest)
    edges = [{'source': left, 'target': right, **possible[(left, right)]}
             for left, right in sorted(retained)]
    selected_set = set(selected)
    # Timeline arrows show knowledge flow: cited work -> the work citing it.
    # Similarity edges above are undirected and cannot supply this direction.
    citation_edges = [{'source': cited, 'target': citing, 'cited': cited, 'citing': citing}
                      for citing in sorted(selected) for cited in sorted(refs[citing] & selected_set)]
    known_ids = set(works)
    lineage_edges = [{'source': cited, 'target': citing}
                     for citing in sorted(works) for cited in sorted(refs[citing] & known_ids)]
    for edge in [*citation_edges, *lineage_edges]:
        detail = next((item for item in works[edge['target']].get('reference_evidence', [])
                       if item.get('target') == edge['source']), None)
        if detail:
            edge.update(reference_source=detail['source'], reference_number=detail.get('number'),
                        reference_page=detail.get('page'))
    prior_counts = Counter(cited for key in selected for cited in refs[key]
                           if cited in works and cited != seed)
    derivative_counts = {key: len(refs[key] & set(selected)) for key in works if key != seed}

    def relation_list(counts):
        return [{**node(key), 'relation_count': count} for key, count in
                sorted(counts.items(), key=lambda item: (-item[1], item[0])) if count][:60]

    metadata = dict(snapshot.get('meta') or {})
    metadata.update(provider='OpenAlex', method=METHOD, fetched_at=snapshot.get('fetched_at'),
                    candidate_count=len(works) - 1, candidate_limit=MAX_WORKS,
                    connected_candidate_count=len(ranking), displayed_count=len(selected),
                    coverage_note=COVERAGE_NOTE, edge_count=len(edges),
                    citation_edge_count=len(citation_edges), citation_edge_direction='cited_to_citing',
                    lineage_node_count=len(works), lineage_edge_count=len(lineage_edges),
                    lineage_scope='saved_neighborhood',
                    coupling_weight=.65, cocitation_weight=.35, direct_citation_bonus=.12,
                    cocitation_scope='downloaded_records',
                    abstract_count=sum(bool(works[key].get('abstract')) for key in selected))
    return {'source': 'citation_similarity', 'seed_id': seed,
            'nodes': [node(key) for key in selected], 'edges': edges, 'citation_edges': citation_edges,
            'lineage_nodes': [node(key) for key in sorted(works)], 'lineage_edges': lineage_edges,
            'prior_works': relation_list(prior_counts),
            'derivative_works': relation_list(derivative_counts), 'meta': metadata}


class CitationGraphService:
    """Bounded jobs avoid blocking Flask workers while remote metadata loads."""
    def __init__(self, provider=None, cache_dir=None, max_workers=2):
        self.provider = provider or OpenAlexProvider()
        version = getattr(self.provider, 'cache_version', 1)
        self.cache_version = version if type(version) is int else 1
        self.cache_dir = Path(cache_dir or Path(__file__).resolve().parents[1] /
                              'py_01_data' / 'connection')
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='citation-map')
        self.lock = threading.RLock()
        self.jobs = {}
        self.failures = OrderedDict()
        self.memory = OrderedDict()

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)

    def _path(self, doi):
        return self.cache_dir / (sha256(doi.encode('utf-8')).hexdigest() + '.json')

    def _load(self, doi):
        cached = self.memory.get(doi)
        if cached and 0 <= time.time() - cached[0] < _cache_ttl(cached[1]):
            self.memory.move_to_end(doi)
            return cached[1]
        self.memory.pop(doi, None)
        try:
            path = self._path(doi)
            if path.stat().st_size > 12_000_000:
                return None
            cached = json.loads(path.read_text(encoding='utf-8'))
            if (isinstance(cached, dict) and cached.get('version') in {1, self.cache_version} and cached.get('doi') == doi and
                    0 <= time.time() - cached['cached_at'] < _cache_ttl(cached['snapshot'])):
                snapshot = _public_snapshot(cached['snapshot'])
                self._remember(doi, cached['cached_at'], snapshot)
                return snapshot
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        return None

    def _remember(self, doi, when, snapshot):
        self.memory[doi] = (when, snapshot)
        self.memory.move_to_end(doi)
        while len(self.memory) > 32:
            self.memory.popitem(last=False)

    def _store(self, doi, snapshot):
        when = time.time()
        with self.lock:
            self._remember(doi, when, snapshot)
        temporary = None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            target = self._path(doi)
            temporary = target.with_suffix('.' + uuid4().hex + '.tmp')
            temporary.write_text(json.dumps({'version': self.cache_version, 'doi': doi, 'cached_at': when,
                                             'snapshot': snapshot}, ensure_ascii=False), encoding='utf-8')
            temporary.replace(target)
            # Remove only this service's hash-named cache files, never other data.
            cached = [path for path in self.cache_dir.glob('*.json')
                      if len(path.stem) == 64 and all(c in '0123456789abcdef' for c in path.stem)]
            if len(cached) > 512:
                for path in sorted(cached, key=lambda item: item.stat().st_mtime)[:len(cached) - 512]:
                    path.unlink(missing_ok=True)
        except OSError:
            logger.warning('Citation cache disk write unavailable; retaining memory cache')
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _build(self, doi, existing=None):
        snapshot = _public_snapshot(self.provider.enrich(existing) if existing is not None else self.provider.fetch(doi))
        if len(snapshot['works']) == 1 and snapshot.get('meta', {}).get('warnings'):
            raise ProviderError('주변 논문의 인용 데이터를 가져오지 못했습니다. 잠시 후 다시 시도해 주세요.')
        if not snapshot.get('fetched_at'):
            snapshot['fetched_at'] = datetime.now(timezone.utc).isoformat()
        self._store(doi, snapshot)
        return snapshot

    def _collect_completed(self):
        # Preserve failures even after a user closes the tab. Otherwise an
        # unrelated request could discard a 429 and evade the provider cooldown.
        for doi in [key for key, job in self.jobs.items() if job.done()]:
            future = self.jobs.pop(doi)
            try:
                future.result()
                continue
            except ProviderError as error:
                message, status = str(error), error.status
            except Exception as error:
                logger.warning('Citation map build failed (%s)', type(error).__name__)
                message, status = '인용 데이터를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.', 503
            until = time.monotonic() + (300 if status == 429 else 60)
            self.failures[doi] = (until, message, status)
            while len(self.failures) > 64:
                self.failures.popitem(last=False)

    def get(self, doi, limit=30, cached_only=False):
        doi = normalize_doi(doi)
        with self.lock:
            self._collect_completed()
            snapshot = self._load(doi)
            upgrade = snapshot is not None and self.cache_version > snapshot.get('meta', {}).get('enrichment_version', 1)
            if snapshot is not None and (not upgrade or cached_only):
                graph = build_citation_graph(snapshot, limit)
                graph['meta']['cache_ttl_seconds'] = _cache_ttl(snapshot)
                graph['meta']['cache_ttl_days'] = _cache_ttl(snapshot) / 86400
                return graph, 200
            if cached_only:
                return {'error': '저장된 인용 데이터가 없습니다. 제목·초록 지도를 이용하거나 추후 외부 조회를 진행하세요.'}, 404
            failed = self.failures.get(doi)
            if failed and time.monotonic() < failed[0]:
                return {'error': failed[1], 'retry_after': max(1, int(failed[0] - time.monotonic()))}, failed[2]
            self.failures.pop(doi, None)
            future = self.jobs.get(doi)
            if future is None:
                if len(self.jobs) >= 8:
                    return {'error': '다른 논문 지도를 생성 중입니다. 잠시 후 다시 시도해 주세요.',
                            'retry_after': 10}, 429
                self.jobs[doi] = self.executor.submit(self._build, doi, snapshot if upgrade else None)
            return {'status': 'building', 'retry_after': 2,
                    'message': '공개 참고문헌과 인용 관계를 수집하고 있습니다.'}, 202
