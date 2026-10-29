"""Account-scoped, per-favorite retrieval over the complete shared title corpus.

The index contains public bibliographic data only. Personal seeds, exclusions,
scores and neighbors are computed per request and never stored in that index.
"""
from collections import defaultdict
from datetime import date
from itertools import zip_longest
from math import log, sqrt
import threading
import time

import numpy as np
from sqlalchemy import bindparam, text
from paper_server.accounts import favorite_sql
from .abstract_expansion import AbstractTitleIndex

from paper_server.recommendation.policy import (
    BROAD_TERMS, CONCEPT_NAMES, excluded_electrical_multicarrier, identity_text,
    near_duplicate, technical_terms,
)

RETRIEVAL_VERSION = 'favorite-neighbors-2'
MIN_NEIGHBOR_SIMILARITY = .18
MAX_POOL = 6000
TITLE_SERIES_TERMS = frozenset({'all', 'seasons', 'part', 'comments'})


def retrieval_terms(title, tokenize):
    # Magazine series/part labels are bibliographic context, not a technical
    # connection. Keep the paper available; just omit those words from scoring.
    return technical_terms(title, tokenize) - TITLE_SERIES_TERMS


class TitleIndex:
    def __init__(self, rows, tokenize, is_low_quality=lambda title: False):
        started = time.monotonic()
        self.tokenize = tokenize
        self.rows = tuple(sorted((dict(row) for row in rows), key=lambda row: str(row['article_number'])))
        postings = defaultdict(list)
        allowed = []
        for i, row in enumerate(self.rows):
            title = row.get('title') or ''
            allowed.append(bool(title and row.get('source_name')) and not is_low_quality(title)
                           and not excluded_electrical_multicarrier(row))
            for term in retrieval_terms(title, tokenize):
                postings[term].append(i)
        count = len(self.rows)
        self.postings = {term: np.asarray(ids, dtype=np.int32) for term, ids in postings.items()}
        self.weights = {term: (1 + log((count + 1) / (len(ids) + 1))) * (.35 if term in BROAD_TERMS else 1)
                        for term, ids in self.postings.items()}
        norms = np.zeros(count, dtype=np.float32)
        for term, ids in self.postings.items():
            norms[ids] += self.weights[term] ** 2
        self.norms = np.sqrt(norms)
        self.allowed = np.asarray(allowed, dtype=bool)
        self.positions = {str(row['article_number']): i for i, row in enumerate(self.rows)}
        self.abstract_index = AbstractTitleIndex(self.rows)
        self.build_seconds = round(time.monotonic() - started, 3)

    def retrieve(self, favorites, excluded=(), limit=MAX_POOL, current_year=None):
        started = time.monotonic()
        current_year = current_year or date.today().year
        seeds = sorted((dict(row) for row in favorites if not excluded_electrical_multicarrier(row)),
                       key=lambda row: str(row['article_number']))
        excluded_ids = set(map(str, excluded)) | {str(row['article_number']) for row in favorites}
        favorite_titles = {identity_text(row['title']) for row in favorites}
        allowed = self.allowed.copy()
        for article in excluded_ids:
            position = self.positions.get(article)
            if position is not None:
                allowed[position] = False
        size = len(self.rows)
        scores = np.zeros(size, dtype=np.float32)
        hits = np.zeros(size, dtype=np.int32)
        specific = np.zeros(size, dtype=bool)
        recency = np.asarray([self._recency(row.get('year'), current_year) for row in self.rows], dtype=np.float32)
        records, queues = {}, []
        searched = found = 0
        for seed in seeds:
            features = retrieval_terms(seed['title'], self.tokenize)
            if len(features) < 2 or not features - BROAD_TERMS:
                continue
            searched += 1
            scores.fill(0)
            hits.fill(0)
            specific.fill(False)
            norm = sqrt(sum(self.weights.get(term, 1 + log(size + 1)) ** 2 for term in features))
            for term in sorted(features):
                positions = self.postings.get(term)
                if positions is None:
                    continue
                scores[positions] += self.weights[term] ** 2
                hits[positions] += 1
                if term not in BROAD_TERMS:
                    specific[positions] = True
            valid = np.flatnonzero(allowed & (hits >= 2) & specific &
                                   (scores >= MIN_NEIGHBOR_SIMILARITY * norm * self.norms) & (self.norms > 0))
            if not len(valid):
                continue
            scores[valid] /= norm * self.norms[valid]
            ranked = self._top(valid, scores, 128)
            # Reject duplicates before quota selection, allowing other neighbors
            # to take their place. Numeric performance differences remain valid.
            ranked = [i for i in ranked if identity_text(self.rows[i]['title']) not in favorite_titles
                      and not near_duplicate(self.rows[i]['title'], seed['title'])]
            nearest = ranked[:8]
            sources = {self.rows[i]['source_name'] for i in nearest}
            adjacent = sorted(ranked[8:], key=lambda i: (self.rows[i]['source_name'] in sources, -float(scores[i]), i))[:4]
            recent_scores = scores[valid] * (.5 + .5 * recency[valid])
            recent_positions = self._top(np.arange(len(valid)), recent_scores, 16)
            recent = [int(valid[j]) for j in recent_positions
                      if identity_text(self.rows[int(valid[j])]['title']) not in favorite_titles
                      and not near_duplicate(self.rows[int(valid[j])]['title'], seed['title'])][:4]
            channels = {'nearest': nearest, 'adjacent': adjacent, 'recent': recent}
            queue = list(dict.fromkeys(i for group in zip_longest(nearest, adjacent, recent)
                                      for i in group if i is not None))
            if queue:
                found += 1
                queues.append(queue)
            for channel, indices in channels.items():
                for i in indices:
                    score = min(1.0, float(scores[i]))
                    record = records.setdefault(i, {'channels': set(), 'seeds': set(), 'score': -1})
                    record['channels'].add(channel)
                    record['seeds'].add(str(seed['article_number']))
                    if score > record['score']:
                        record.update(score=score, seed=seed)
        # Give every searchable favorite a first neighbor before admitting more
        # from prolific interests. Shared neighbors occupy only one pool slot.
        retained, seen, seen_titles = [], set(), set()
        for depth in range(max(map(len, queues), default=0)):
            for queue in queues:
                if depth < len(queue) and queue[depth] not in seen:
                    seen.add(queue[depth])
                    title_key = identity_text(self.rows[queue[depth]]['title'])
                    if title_key in seen_titles:
                        continue
                    seen_titles.add(title_key)
                    retained.append(queue[depth])
                    if len(retained) >= max(0, limit):
                        break
            if len(retained) >= max(0, limit):
                break
        if limit <= 0:
            retained = []
        output = []
        for i in retained:
            record, row = records[i], self.rows[i]
            seed = record['seed']
            shared = retrieval_terms(row['title'], self.tokenize) & retrieval_terms(seed['title'], self.tokenize)
            output.append({**row, 'favorite_similarity': round(record['score'], 4),
                'related_favorite_id': str(seed['article_number']), 'related_favorite_title': seed['title'],
                'shared_terms': sorted(set(self.tokenize(row['title'])) & set(self.tokenize(seed['title']))),
                'shared_concepts': sorted(shared & CONCEPT_NAMES), 'shared_technical_terms': sorted(shared - BROAD_TERMS),
                'retrieval_channels': sorted(record['channels']), 'retrieval_support': len(record['seeds']),
                'retrieval_basis': RETRIEVAL_VERSION})
        return output, {'version': RETRIEVAL_VERSION, 'corpus_count': size,
            'favorite_count': len(favorites), 'searched_favorites': searched, 'favorites_with_neighbors': found,
            'discovered_candidates': len(records), 'pool_count': len(output),
            'index_build_seconds': self.build_seconds, 'search_seconds': round(time.monotonic() - started, 3)}

    @staticmethod
    def _top(indices, scores, count):
        if len(indices) > count:
            values = scores[indices]
            threshold = np.partition(values, len(values) - count)[len(values) - count]
            # Include ties at the boundary then resolve by stable corpus order.
            indices = indices[values >= threshold]
        return sorted(map(int, indices), key=lambda i: (-float(scores[i]), i))[:count]

    @staticmethod
    def _recency(value, current_year):
        try:
            year = int(str(value)[:4])
        except ValueError:
            return 0
        return max(0, 1 - max(0, current_year - year) * .08)


class TitleIndexCache:
    """Share only the corpus, rebuilding on additions/removals or after 30 min."""
    def __init__(self, ttl=1800):
        self.ttl = ttl
        self.lock = threading.Lock()
        self.key = self.index = None
        self.created = 0

    def get(self, key, loader, tokenize, is_low_quality):
        with self.lock:
            if self.key != key or self.index is None or time.monotonic() - self.created >= self.ttl:
                index = TitleIndex(loader(), tokenize, is_low_quality)
                self.index, self.key, self.created = index, key, time.monotonic()
            return self.index


_CORPUS_INDEX = TitleIndexCache()


def retrieve_corpus_candidates(engine, favorites, excluded, tokenize, is_low_quality, current_year):
    with engine.connect() as conn:
        state = conn.execute(text('SELECT COUNT(*) n,MAX(id) latest FROM papers')).one()
    def load():
        with engine.connect() as conn:
            return conn.execute(text('SELECT id,article_number,title,source_name,year FROM papers')).mappings().all()
    index = _CORPUS_INDEX.get((engine, *state), load, tokenize, is_low_quality)
    candidates, stats = index.retrieve(favorites, excluded, current_year=current_year)
    # Canonical saved abstracts are query inputs ONLY for this account's
    # favorite seeds. They are never retained in the shared corpus cache.
    with engine.connect() as conn:
        abstracts = {str(row.article_number): row.abstract_text for row in conn.execute(text(
            'SELECT p.article_number,a.abstract_text FROM papers p '
            'JOIN paper_current_abstracts a ON a.paper_id=p.id '
            f'WHERE {favorite_sql("p")}=1 AND a.is_current=1 AND a.abstract_text IS NOT NULL'))}
    supplemental, abstract_stats = index.abstract_index.supplement(index, favorites, abstracts, candidates, excluded)
    stats.update(abstract_stats, title_pool_count=len(candidates))
    candidates = candidates + supplemental
    # Recheck account-owned favorites against current SQL state and fetch only
    # the retained pool's complete metadata. The shared index has no user state.
    current = {}
    statement = text('SELECT id,article_number,title,authors,year,source_name,source_type,source_system,'
        f'issue,url,pdf_available,{favorite_sql()} AS is_favorite FROM papers '
        f'WHERE id IN :ids AND {favorite_sql()}=0').bindparams(bindparam('ids', expanding=True))
    with engine.connect() as conn:
        for start in range(0, len(candidates), 500):
            rows = conn.execute(statement, {'ids': [row['id'] for row in candidates[start:start+500]]}).mappings()
            current.update({row['id']: dict(row) for row in rows})
    items = [{**row, **current[row['id']]} for row in candidates if row['id'] in current
             and current[row['id']]['title'] == row['title']]
    stats['pool_count'] = len(items)
    stats['abstract_added_candidates'] = sum(row.get('retrieval_channels') == ['abstract'] for row in items)
    stats['title_pool_count'] = len(items) - stats['abstract_added_candidates']
    return items, stats
