"""Research maps over existing SQL metadata and public citation evidence.

Account favorites are resolved on every request and never enter shared caches.
The original SQL-only lexical mode remains available without a remote provider.
"""
from collections import Counter
from itertools import combinations
from math import log, sqrt
import re

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import bindparam, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from paper_server.accounts import favorite_sql
from paper_server.citation_graph import CitationGraphService
from paper_server.citation_provider import normalize_doi
from paper_server.recommendation.abstract_expansion import concepts
from paper_server.recommendation.policy import (
    BROAD_TERMS, expanded_search_terms, technical_terms,
)


MAX_CANDIDATES = 500
MAX_GRAPH_NODES = 60
MIN_SIMILARITY = .18
MAX_ABSTRACT_CHARS = 12000
METHOD = 'title_weighted_technical_term_cosine_v1'
# A shared fabrication platform or performance adjective does not establish a
# shared research function (e.g. FinFET RF amplifiers versus wireline SerDes).
# Keep these as weak context so device-focused titles can still match through
# their other specific terms; do not apply recommendation topic exclusions.
CONTEXT_TERMS = BROAD_TERMS | frozenset('''finfet fdsoi soi bicmos sige gan gaas
process processes technology technologies efficiency energy consumption area
compact scalable scaling scale fast broadband wideband ultralow ultra'''.split())
STOPWORDS = frozenset('''a an the of in on at to for and or with using based via
from into towards toward by is are be this that these those its their new novel
high low performance design analysis study approach method system systems model
models paper review under over between as we propose proposed present presented
results result effect effects case characterization fabrication implementation
application applications improved improving efficient publisher author correction
editorial erratum per all seasons part comments demonstrates demonstrate achieves
achieved measured measurement measurements experimental experiments experiment
first second third also can has have been was were which such than through each
used use uses both shown show shows respectively compared conventional previous
prior work works technique techniques et al fig figure table well total maximum
minimum wide small large various different development developed includes include
including provides provide two three four five one recent existing article
gbps gb gbits mbps mhz ghz khz nm db ps fs ns uw mw pj fj ms us'''.split())
PUBLIC_COLUMNS = ('article_number', 'title', 'authors', 'year', 'source_name',
                  'source_system', 'doi', 'pdf_available', 'citation_count',
                  'citation_source', 'citation_updated_at')
SELECT_COLUMNS = 'p.id AS db_id,' + ','.join('p.' + name for name in PUBLIC_COLUMNS)


def _tokenize(value):
    return {word for word in re.findall(r'[a-z][a-z0-9]*|[가-힣]{2,}', value.casefold())
            if len(word) >= 3 and word not in STOPWORDS}


def _features(row, abstract=''):
    """Titles lead; only recognized, grounded abstract concepts supplement them.

    The existing concept extractor omits explicit absence and comparison prose.
    Technical aliases (e.g. DFE / decision feedback equalizer) are normalized by
    the shared policy helper, without applying personal recommendation filters.
    """
    title = technical_terms(str(row.get('title') or ''), _tokenize)
    features = {term: 1.0 for term in sorted(title)}
    for concept in list(concepts(abstract[:MAX_ABSTRACT_CHARS], abstract=True))[:24]:
        for term in sorted(technical_terms(concept, _tokenize)):
            features.setdefault(term, .35)
    return features


def _paper(row):
    item = {name: row.get(name) for name in PUBLIC_COLUMNS}
    item['article_number'] = str(item['article_number'])
    item['title'] = str(item.get('title') or '')
    item['pdf_available'] = bool(item.get('pdf_available'))
    item['is_favorite'] = bool(row.get('is_favorite'))
    item.update(_citation_fields(row))
    return item


def _citation_fields(row):
    count = row.get('citation_count')
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return {'citation_count': None, 'citation_source': None, 'citation_updated_at': None}
    when = row.get('citation_updated_at')
    if hasattr(when, 'isoformat'):
        when = when.isoformat() + ('Z' if getattr(when, 'tzinfo', None) is None else '')
    return {'citation_count': count, 'citation_source': row.get('citation_source'),
            'citation_updated_at': when}


def _weights(features):
    counts = Counter(term for document in features for term in document)
    return {term: (1 + log((len(features) + 1) / (count + 1))) *
            (.3 if term in CONTEXT_TERMS else 1) for term, count in counts.items()}


def _similarity(left, right, weights):
    shared = left.keys() & right.keys()
    # Generic labels alone ("optical receiver") must never create an edge.
    if len(shared) < 2 or not shared - CONTEXT_TERMS:
        return 0.0, []
    left_norm = sqrt(sum((left[term] * weights[term]) ** 2 for term in sorted(left)))
    right_norm = sqrt(sum((right[term] * weights[term]) ** 2 for term in sorted(right)))
    if not left_norm or not right_norm:
        return 0.0, []
    score = min(1.0, sum(left[term] * right[term] * weights[term] ** 2
                         for term in sorted(shared)) / (left_norm * right_norm))
    if score < MIN_SIMILARITY:
        return 0.0, []
    return score, sorted(shared, key=lambda term: (-weights[term], term))[:10]


def build_graph(seed, candidates, abstracts, limit=30, abstracts_supported=True):
    """Rank seed neighbors, then connect displayed pairs using the same rule."""
    rows = [seed]
    seen = {str(seed['article_number'])}
    for row in candidates[:MAX_CANDIDATES]:
        article = str(row.get('article_number') or '')
        if article and article not in seen:
            seen.add(article)
            rows.append(row)
    features = [_features(row, abstracts.get(row['db_id'], '')) for row in rows]
    weights = _weights(features)
    neighbors = []
    for index in range(1, len(rows)):
        score, shared = _similarity(features[0], features[index], weights)
        if score:
            neighbors.append((index, score, shared))
    neighbors.sort(key=lambda entry: (-entry[1], str(rows[entry[0]]['article_number'])))
    selected = [(0, 1.0, []), *neighbors[:max(0, limit - 1)]]
    nodes, edges = [], []
    for index, score, shared in selected:
        row = rows[index]
        nodes.append({**_paper(row), 'id': str(row['article_number']),
                      'similarity': round(score, 4), 'shared_terms': shared,
                      'is_seed': index == 0,
                      'abstract_available': bool(abstracts.get(row['db_id'], '').strip())})
    for (left, _, _), (right, _, _) in combinations(selected, 2):
        score, shared = _similarity(features[left], features[right], weights)
        if score:
            edges.append({'source': str(rows[left]['article_number']),
                          'target': str(rows[right]['article_number']),
                          'weight': round(score, 4), 'shared_terms': shared})
    return {'source': 'local_similarity', 'seed_id': str(seed['article_number']),
            'nodes': nodes, 'edges': edges,
            'meta': {'method': METHOD, 'candidate_count': len(rows) - 1,
                     'candidate_limit': MAX_CANDIDATES,
                     'candidate_limit_reached': len(candidates) >= MAX_CANDIDATES,
                     'abstract_count': sum(node['abstract_available'] for node in nodes),
                     'abstracts_supported': abstracts_supported,
                     'minimum_similarity': MIN_SIMILARITY,
                     'title_weight': 1.0, 'abstract_concept_weight': .35}}


def _like(value):
    return '%' + value.replace('=', '==').replace('%', '=%').replace('_', '=_') + '%'


def _missing_abstracts(error):
    original = getattr(error, 'orig', None)
    args = getattr(original, 'args', ())
    return (bool(args) and args[0] == 1146) or (
        'no such table: paper_current_abstracts' in str(original).lower())


class ConnectionRepository:
    def __init__(self, engine):
        self.engine = engine

    @property
    def mysql(self):
        return self.engine.dialect.name in {'mysql', 'mariadb'}

    def _select(self):
        return SELECT_COLUMNS + f', {favorite_sql("p")} AS is_favorite'

    def seed_doi(self, article):
        with self.engine.connect() as conn:
            row = conn.execute(text('SELECT doi FROM papers WHERE article_number=:article LIMIT 1'),
                               {'article': article}).mappings().first()
        return row

    def hydrate_citation_graph(self, graph):
        """Join public graph metadata to this request's library/favorite scope."""
        all_nodes = [*graph['nodes'], *graph.get('prior_works', []),
                     *graph.get('derivative_works', []), *graph.get('lineage_nodes', [])]
        dois = sorted({node['doi'] for node in all_nodes if node.get('doi')})[:180]
        by_doi, abstracts = {}, {}
        if dois:
            statement = text(f'SELECT {self._select()} FROM papers p WHERE p.doi IN :dois '
                             "AND p.article_number IS NOT NULL AND p.article_number<>'' LIMIT 180").bindparams(
                                 bindparam('dois', expanding=True))
            # MySQL's DOI index uses a case-insensitive collation. SQLite test
            # fixtures use NOCASE to preserve the same identifier semantics.
            if not self.mysql:
                statement = text(f'SELECT {self._select()} FROM papers p '
                                 "WHERE p.doi COLLATE NOCASE IN :dois AND p.article_number IS NOT NULL "
                                 "AND p.article_number<>'' LIMIT 180").bindparams(bindparam('dois', expanding=True))
            with self.engine.connect() as conn:
                rows = conn.execute(statement, {'dois': dois}).mappings().all()
                for row in rows:
                    try:
                        by_doi[normalize_doi(row['doi'])] = row
                    except ValueError:
                        continue
                abstracts, _ = self._abstracts(conn, [row['db_id'] for row in rows])
        for node in all_nodes:
            row = by_doi.get(node.get('doi'))
            node.update(article_number=None, in_library=False, pdf_available=False, is_favorite=False)
            if row is not None:
                node.update(article_number=str(row['article_number']), in_library=True,
                            pdf_available=bool(row['pdf_available']), is_favorite=bool(row['is_favorite']))
                for key in ('title', 'authors', 'year', 'source_name'):
                    if row.get(key):
                        node[key] = row[key]
                if not node.get('abstract') and abstracts.get(row['db_id']):
                    node['abstract'] = abstracts[row['db_id']]
                # Keep OpenAlex's count/source together. A known zero is valid;
                # use the saved publisher count only when OpenAlex is unknown.
                if node.get('citation_count') is None:
                    node.update(_citation_fields(row))
            node['abstract_available'] = bool(node.get('abstract'))
        graph['meta']['library_count'] = sum(node['in_library'] for node in graph['nodes'])
        graph['meta']['abstract_count'] = sum(node['abstract_available'] for node in graph['nodes'])
        return graph

    def search(self, query, limit):
        params = {'limit': limit + 1}
        where = "p.article_number IS NOT NULL AND p.article_number<>''"
        order = 'is_favorite DESC,p.year DESC,p.id DESC'
        if query:
            params.update(query=query, exact=query, like_query=_like(query))
            exact = '(p.article_number=:exact OR p.doi=:exact)'
            # Identifier fragments are literal; bibliographic words use the
            # existing full-text index. Short acronyms need LIKE because MySQL
            # may exclude them through its minimum token length.
            if self.mysql and len(query) >= 3 and re.search(r'[A-Za-z]{3}', query):
                match = 'MATCH(p.title,p.authors) AGAINST(:query IN NATURAL LANGUAGE MODE)'
                where += (f' AND ({exact} OR {match}>0 '
                          "OR p.article_number LIKE :like_query ESCAPE '=' "
                          "OR p.doi LIKE :like_query ESCAPE '=')")
                order = f'{exact} DESC,{match} DESC,p.year DESC,p.id DESC'
            else:
                where += (f' AND ({exact} OR p.title LIKE :like_query ESCAPE \'=\' '
                          "OR p.authors LIKE :like_query ESCAPE '=' "
                          "OR p.doi LIKE :like_query ESCAPE '=' "
                          "OR p.article_number LIKE :like_query ESCAPE '=')")
                order = f'{exact} DESC,p.year DESC,p.id DESC'
        with self.engine.connect() as conn:
            rows = conn.execute(text(f'SELECT {self._select()} FROM papers p '
                                     f'WHERE {where} ORDER BY {order} LIMIT :limit'), params).mappings().all()
        return {'papers': [_paper(row) for row in rows[:limit]], 'has_more': len(rows) > limit}

    def _abstracts(self, conn, ids):
        if not ids:
            return {}, True
        statement = text('SELECT paper_id,SUBSTR(abstract_text,1,:max_chars) AS abstract_text '
                         'FROM paper_current_abstracts WHERE paper_id IN :ids '
                         'AND is_current=1 AND abstract_text IS NOT NULL').bindparams(
                             bindparam('ids', expanding=True))
        try:
            rows = conn.execute(statement, {'ids': ids, 'max_chars': MAX_ABSTRACT_CHARS}).mappings()
            return {row['paper_id']: str(row['abstract_text'] or '') for row in rows}, True
        except DBAPIError as error:
            if _missing_abstracts(error):
                return {}, False
            raise

    def graph(self, article, limit):
        with self.engine.connect() as conn:
            seed = conn.execute(text(f'SELECT {self._select()} FROM papers p '
                                     'WHERE p.article_number=:article LIMIT 1'),
                                {'article': article}).mappings().first()
            if seed is None:
                return None
            abstracts, supported = self._abstracts(conn, [seed['db_id']])
            seed_features = _features(seed, abstracts.get(seed['db_id'], ''))
            # Keep the title's strongest terms before adding bounded abstract
            # concepts. Only query these indexed titles; never scan all abstracts.
            terms = sorted(seed_features, key=lambda term: (
                -seed_features[term], term in CONTEXT_TERMS, term))[:24]
            lexical = expanded_search_terms(terms)[:40]
            candidates = []
            if lexical:
                params = {'article': article, 'limit': MAX_CANDIDATES}
                if self.mysql:
                    params['query'] = ' '.join(lexical)
                    match = 'MATCH(p.title,p.authors) AGAINST(:query IN NATURAL LANGUAGE MODE)'
                    condition, order = f'{match}>0', f'{match} DESC,p.id DESC'
                else:
                    # SQLite's LIKE branch permits completely offline tests and
                    # small local fixtures; production uses the existing index.
                    clauses = []
                    for index, term in enumerate(lexical):
                        params[f'term{index}'] = _like(term)
                        clauses.append(f"p.title LIKE :term{index} ESCAPE '='")
                    condition, order = '(' + ' OR '.join(clauses) + ')', 'p.id DESC'
                candidates = conn.execute(text(f'SELECT {self._select()} FROM papers p '
                    'WHERE p.article_number IS NOT NULL AND p.article_number<>:article '
                    f'AND {condition} ORDER BY {order} LIMIT :limit'), params).mappings().all()
                if supported:
                    additional, supported = self._abstracts(conn, [row['db_id'] for row in candidates])
                    abstracts.update(additional)
        return build_graph(seed, candidates, abstracts, limit, supported)


def _limit(default, maximum):
    value = request.args.get('limit', str(default))
    if not re.fullmatch(r'[0-9]{1,3}', value) or not 1 <= int(value) <= maximum:
        raise ValueError(f'limit은 1~{maximum} 사이의 정수여야 합니다.')
    return int(value)


def create_connection_blueprint(engine, citation_service=None):
    blueprint = Blueprint('connection', __name__)
    repository = ConnectionRepository(engine)
    if citation_service is None:
        from paper_server.citation_fallback import CitationLibrary, SupplementedCitationProvider
        citation_service = CitationGraphService(SupplementedCitationProvider(CitationLibrary(engine)))
    citations = citation_service

    @blueprint.after_request
    def private_response(response):
        # Responses include account-owned favorite flags, including on errors.
        response.headers['Cache-Control'] = 'private, no-store'
        return response

    @blueprint.errorhandler(SQLAlchemyError)
    def database_error(error):
        current_app.logger.error('Connection map SQL lookup failed (%s)', type(error).__name__)
        return jsonify(error='논문 정보를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.'), 503

    @blueprint.get('/api/connection/search')
    def search():
        try:
            limit = _limit(20, 50)
            query = request.args.get('q', '').strip()
            if len(query) > 200 or re.search(r'[\x00-\x1f\x7f]', query):
                raise ValueError('검색어는 제어문자 없이 200자 이내로 입력하세요.')
        except ValueError as error:
            return jsonify(error=str(error)), 400
        return jsonify(repository.search(query, limit))

    @blueprint.get('/api/connection/graph')
    def graph():
        try:
            limit = _limit(30, MAX_GRAPH_NODES)
            mode = request.args.get('mode', 'local')
            if mode not in ('local', 'citation'):
                raise ValueError('mode는 local 또는 citation이어야 합니다.')
            cached_only = request.args.get('cached_only', '0')
            if cached_only not in ('0', '1'):
                raise ValueError('cached_only는 0 또는 1이어야 합니다.')
            article = request.args.get('article_number', '').strip()
            if len(article) > 255 or re.search(r'[\x00-\x1f\x7f]', article):
                raise ValueError('유효한 논문 식별자(article_number)를 지정하세요.')
            doi = request.args.get('doi', '').strip()
            if mode == 'citation' and doi:
                doi = normalize_doi(doi)
            elif not article:
                raise ValueError('유효한 논문 식별자(article_number)를 지정하세요.')
        except ValueError as error:
            return jsonify(error=str(error)), 400
        if mode == 'citation':
            if article:
                seed = repository.seed_doi(article)
                if seed is None:
                    return jsonify(error='선택한 논문이 보관함에 없습니다.'), 404
                try:
                    doi = normalize_doi(seed['doi'] or '')
                except ValueError:
                    return jsonify(error='이 논문에는 조회 가능한 DOI가 없습니다. 제목·초록 유사도 지도를 이용하세요.'), 422
            result, status = citations.get(doi, limit, cached_only=cached_only == '1')
            if status == 200:
                result = repository.hydrate_citation_graph(result)
            response = jsonify(result)
            response.status_code = status
            if result.get('retry_after'):
                response.headers['Retry-After'] = str(result['retry_after'])
            return response
        result = repository.graph(article, limit)
        if result is None:
            return jsonify(error='선택한 논문이 보관함에 없습니다.'), 404
        return jsonify(result)

    return blueprint
