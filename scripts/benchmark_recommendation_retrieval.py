"""Read-only comparison of the real account's retrieval and deployed lists.

Only local diagnostic JSON is written. Session signing stays in memory and is
restricted to the loopback server; no credentials or cookies are printed.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import requests
from sqlalchemy import text
import web.app as server
from paper_server.accounts import account_scope
from paper_server.ai.recommendations import select_sql_mode
from paper_server.recommendation.retrieval import RETRIEVAL_VERSION
from paper_server.recommendation.policy import excluded_electrical_multicarrier, identity_text

OUT = ROOT / 'outputs/recommendation_retrieval_v6'
MODES = ('match', 'explore', 'recent', 'diverse')


def summarize(items):
    return {'items': len(items), 'sources': dict(Counter(p['source_name'] for p in items)),
            'topics': dict(Counter(p.get('interest_topic', 'unknown') for p in items)),
            'anchors': len({p.get('related_favorite_id') for p in items}),
            'mean_similarity': round(sum(p.get('favorite_similarity', 0) for p in items) / max(1, len(items)), 4)}


def main():
    global OUT
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=['baseline', 'candidate', 'deployed'])
    parser.add_argument('--output-dir', type=Path, default=OUT)
    args = parser.parse_args()
    OUT = args.output_dir
    with server.engine.connect() as conn:
        account = dict(conn.execute(text("SELECT id,username,role,session_version FROM accounts "
            "WHERE legacy_owner=1 AND is_active=1")).mappings().one())
    with account_scope(account['id']):
        snap = server.RecommendationService(server.engine, None, server.build_sql_recommendation_pool,
                                           account_id=account['id']).snapshot()
        with server.engine.connect() as conn:
            corpus = dict(conn.execute(text('SELECT COUNT(*) n,MAX(id) latest FROM papers')).mappings().one())
        favorite_signature = hashlib.sha256(json.dumps(snap['favorites'], sort_keys=True, default=str).encode()).hexdigest()
        start = time.perf_counter()
        if args.stage == 'deployed':
            client = requests.Session()
            client.trust_env = False
            cookie = server.app.session_interface.get_signing_serializer(server.app).dumps({
                'authenticated': True, 'account_id': account['id'], 'username': account['username'],
                'role': account['role'], 'session_version': account['session_version']})
            client.cookies.set(server.app.config['SESSION_COOKIE_NAME'], cookie, domain='127.0.0.1', path='/')
            last_status = None
            for attempt in range(150):
                response = client.get('http://127.0.0.1:5001/api/recommendations?mode=match', timeout=30)
                response.raise_for_status()
                base = response.json()
                status = base.get('refresh', {}).get('status')
                if status == 'ready':
                    break
                if status != last_status:
                    print(json.dumps({'stage': 'deployed', 'status': status}), flush=True)
                    last_status = status
                if status == 'failed':
                    raise RuntimeError('Deployed retrieval failed')
                time.sleep(2)
            else:
                raise RuntimeError('Deployed retrieval timed out')
            views = {}
            for mode in MODES:
                response = client.get('http://127.0.0.1:5001/api/recommendations', params={'mode': mode}, timeout=30)
                response.raise_for_status()
                views[mode] = response.json()
            lists = {mode: view['items'] for mode, view in views.items()}
            favorite_ids = {str(p['article_number']) for p in snap['favorites']}
            for mode, view in views.items():
                items = view['items']
                assert view['recommendation_strategy'] == RETRIEVAL_VERSION
                assert view['refresh']['status'] == 'ready' and view['engine'] == 'sql'
                assert len(items) <= 180 and max(Counter(p['source_name'] for p in items).values(), default=0) <= 20
                assert len({p['article_number'] for p in items}) == len(items)
                assert len({identity_text(p['title']) for p in items}) == len(items)
                assert all(str(p['article_number']) not in favorite_ids and not p['is_favorite'] for p in items)
                assert all(p['related_favorite_id'] in favorite_ids for p in items)
                assert all(str(p['article_number']) not in snap['feedback']['actions'] for p in items)
                assert not any(excluded_electrical_multicarrier(p) for p in items)
            response = client.get('http://127.0.0.1:5001/api/recommendations?mode=survey', timeout=30)
            response.raise_for_status()
            assert response.json()['recommendation_strategy'] == RETRIEVAL_VERSION
        else:
            base = server.build_sql_recommendation_pool(20)
            lists = {mode: select_sql_mode(base['items'], mode, 20, base.get('favorite_topic_counts')) for mode in MODES}
        elapsed = time.perf_counter() - start
    report = {'stage': args.stage, 'account_id': account['id'], 'corpus': corpus,
              'favorites': len(snap['favorites']), 'favorite_signature': favorite_signature,
              'seconds': round(elapsed, 3), 'retrieval': base.get('retrieval', {}),
              'pool_count': base.get('retrieval', {}).get('pool_count', len(base['items'])),
              'modes': {mode: summarize(rows) for mode, rows in lists.items()},
              'lists': lists}
    if args.stage != 'deployed':
        report['pool'] = base['items']
    if args.stage != 'baseline':
        previous = json.loads((OUT / 'baseline.json').read_text(encoding='utf-8'))
        report['same_inputs'] = previous['favorite_signature'] == favorite_signature and previous['corpus'] == corpus
        report['new_to_baseline'] = {mode: len({p['article_number'] for p in lists[mode]} -
            {p['article_number'] for p in previous['lists'][mode]}) for mode in MODES}
        report['unique_across_modes'] = len({p['article_number'] for rows in lists.values() for p in rows})
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f'{args.stage}.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key not in {'lists', 'pool'}}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
