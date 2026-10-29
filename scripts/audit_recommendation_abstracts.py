"""Read-only coverage audit and small, paired abstract query-expansion pilot.

Uses saved text only. Does not fetch abstracts, change production ranking, or
write to SQL. New matches are candidates for review, not relevance labels.
"""
from collections import Counter, defaultdict
from itertools import zip_longest
import json
from math import sqrt
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
from sqlalchemy import text
import web.app as server
from paper_server.accounts import account_scope, favorite_sql
from paper_server.recommendation.feedback import FeedbackStore
from paper_server.recommendation.policy import BROAD_TERMS, classify_interest_topic, identity_text, near_duplicate
from paper_server.recommendation.retrieval import TitleIndex, retrieval_terms

OUTPUT = ROOT / 'outputs/recommendation_abstract_audit'
EXTRA_STOP = {'achieves', 'achieved', 'demonstrated', 'demonstrates', 'respectively', 'while',
              'results', 'measured', 'measurement', 'measurements', 'also', 'than', 'both',
              'which', 'these', 'this', 'proposed', 'paper', 'work', 'have', 'has', 'been',
              'can', 'are', 'was', 'were', 'two', 'three', 'four', 'first', 'second'}


def main():
    start = time.perf_counter()
    with server.engine.connect() as conn:
        account_id = conn.execute(text('SELECT id FROM accounts WHERE legacy_owner=1 AND is_active=1')).scalar_one()
    with account_scope(account_id):
        with server.engine.connect() as conn:
            corpus = [dict(r) for r in conn.execute(text(
                f'SELECT id,article_number,title,source_name,year,{favorite_sql()} AS is_favorite FROM papers')).mappings()]
            abstracts = [dict(r) for r in conn.execute(text(
                'SELECT p.id,p.article_number,p.title,p.source_name,p.year,a.abstract_text,a.provider,a.retrieved_at '
                'FROM paper_current_abstracts a JOIN papers p ON p.id=a.paper_id '
                'WHERE a.is_current=1 AND CHAR_LENGTH(TRIM(a.abstract_text))>0 ORDER BY p.article_number')).mappings()]
        feedback = FeedbackStore(server.engine).read()['actions']
    favorites = [r for r in corpus if r['is_favorite']]
    favorite_ids = {str(r['article_number']) for r in favorites}
    favorite_titles = {identity_text(r['title']) for r in favorites}
    abstract_ids = {str(r['article_number']) for r in abstracts}
    favorite_abstracts = [r for r in abstracts if str(r['article_number']) in favorite_ids]
    lengths = [len(r['abstract_text'].strip()) for r in abstracts]
    counts = {'papers': len(corpus), 'favorites': len(favorites), 'nonfavorites': len(corpus)-len(favorites),
              'current_abstract_rows': len(abstracts), 'papers_with_abstract': len(abstract_ids),
              'favorites_with_abstract': len(favorite_ids & abstract_ids),
              'nonfavorites_with_abstract': len(abstract_ids - favorite_ids),
              'favorite_coverage_percent': round(100*len(favorite_ids & abstract_ids)/max(1,len(favorites)), 2),
              'corpus_coverage_percent': round(100*len(abstract_ids)/max(1,len(corpus)), 2),
              'providers': dict(Counter(r['provider'] for r in abstracts)),
              'sources': dict(Counter(r['source_name'] for r in abstracts)),
              'length_chars': {'min': min(lengths, default=0), 'median': float(np.median(lengths)) if lengths else 0,
                               'max': max(lengths, default=0), 'under_200': sum(n<200 for n in lengths)},
              'duplicate_abstract_texts': sum(n-1 for n in Counter(r['abstract_text'].strip() for r in abstracts).values()),
              'html_markup_rows': sum(bool(re.search(r'<[a-zA-Z][^>]*>', r['abstract_text'])) for r in abstracts)}
    print(json.dumps({'coverage': counts}, ensure_ascii=False), flush=True)
    index = TitleIndex(corpus, server._title_terms, server._is_low_quality_title)
    eligible = index.allowed.copy()
    for article in favorite_ids | set(feedback):
        i = index.positions.get(article)
        if i is not None:
            eligible[i] = False
    grouped = defaultdict(list)
    for row in favorite_abstracts:
        if len(row['abstract_text'].strip()) >= 200:
            grouped[classify_interest_topic(row)].append(row)
    sample = [row for batch in zip_longest(*(grouped[key] for key in sorted(grouped)))
              for row in batch if row is not None][:16]

    def neighbors(seed, extra):
        title_terms = retrieval_terms(seed['title'], server._title_terms)
        weights = {term: 1.0 for term in title_terms}
        weights.update({term: .35 for term in extra if term not in title_terms})
        scores = np.zeros(len(corpus), dtype=np.float32)
        hits = np.zeros(len(corpus), dtype=np.int32)
        specific = np.zeros(len(corpus), dtype=bool)
        norm = sqrt(sum((index.weights.get(term, 1.0) * factor)**2 for term, factor in weights.items()))
        if not norm:
            return []
        for term, factor in sorted(weights.items()):
            positions = index.postings.get(term)
            if positions is None:
                continue
            scores[positions] += factor * index.weights[term]**2
            hits[positions] += 1
            if term not in BROAD_TERMS:
                specific[positions] = True
        valid = np.flatnonzero(eligible & (hits>=2) & specific & (index.norms>0))
        scores[valid] /= norm*index.norms[valid]
        valid = valid[scores[valid]>=.18]
        results = []
        seen = set()
        for i in index._top(valid, scores, 64):
            row = index.rows[i]
            identity = identity_text(row['title'])
            if identity in favorite_titles or identity in seen or near_duplicate(row['title'], seed['title']):
                continue
            shared_extra = sorted(retrieval_terms(row['title'], server._title_terms) & set(extra))
            results.append({'article_number': row['article_number'], 'title': row['title'], 'source': row['source_name'],
                            'score': round(float(scores[i]), 4), 'shared_abstract_only_terms': shared_extra})
            seen.add(identity)
            if len(results)==10:
                break
        return results

    pilot = []
    for seed in sample:
        title_terms = retrieval_terms(seed['title'], server._title_terms)
        # Retain only words actually present in this saved abstract AND in corpus
        # titles. A bounded eight-term supplement limits background-text noise.
        abstract_terms = retrieval_terms(seed['abstract_text'], server._title_terms) - title_terms - BROAD_TERMS - EXTRA_STOP
        extra = sorted((term for term in abstract_terms if term in index.postings and len(index.postings[term])>=3),
                       key=lambda term: (-index.weights[term], term))[:8]
        before, after = neighbors(seed, []), neighbors(seed, extra)
        before_ids = {r['article_number'] for r in before}
        new = [r for r in after if r['article_number'] not in before_ids and r['shared_abstract_only_terms']]
        pilot.append({'favorite_id': seed['article_number'], 'favorite_title': seed['title'],
                      'topic': classify_interest_topic(seed), 'provider': seed['provider'],
                      'abstract_expansion_terms': extra, 'title_only_top10': before,
                      'expanded_top10': after, 'new_candidates_with_abstract_term_evidence': new})
    before_ids = {r['article_number'] for p in pilot for r in p['title_only_top10']}
    after_ids = {r['article_number'] for p in pilot for r in p['expanded_top10']}
    report = {'coverage': counts, 'pilot_summary': {'sample_favorites': len(pilot),
        'sample_selection': 'Deterministic round robin by title topic, then article ID; abstracts >=200 characters.',
        'title_only_unique_candidates': len(before_ids), 'expanded_unique_candidates': len(after_ids),
        'new_to_sample_title_only_union': len(after_ids-before_ids),
        'seeds_with_new_grounded_candidates': sum(bool(p['new_candidates_with_abstract_term_evidence']) for p in pilot),
        'abstract_term_weight': .35, 'extra_terms_per_seed_max': 8,
        'scope': 'Small paired query-expansion pilot, not full-corpus recall or human-judged recommendation quality.'},
        'seconds': round(time.perf_counter()-start, 3), 'production_changed': False, 'pilot': pilot}
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT/'audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='pilot'}, ensure_ascii=False), flush=True)


if __name__=='__main__':
    main()
