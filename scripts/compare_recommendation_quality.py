"""Read-only DB comparison; write only local experimental snapshots.

Run baseline before changing the recommendation code, then candidate afterwards.
Counts/overlaps are diagnostics, not a substitute for user relevance labels.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sqlalchemy import text
import web.app as app
from local_ai_recommendations import select_mode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=['baseline', 'candidate'])
    args = parser.parse_args()
    output = ROOT / 'outputs' / 'recommendation_quality'
    output.mkdir(parents=True, exist_ok=True)
    with app.engine.connect() as conn:
        favorites = [dict(r) for r in conn.execute(text(
            'SELECT article_number,title,source_name FROM papers WHERE is_favorite=1 ORDER BY article_number')).mappings()]
        corpus = dict(conn.execute(text('SELECT COUNT(*) n,MAX(id) latest FROM papers')).mappings().one())
        # Fetch IDs first: sorting large JSON rows can exceed MySQL's sort buffer.
        job_id = conn.execute(text("SELECT id FROM ai_jobs WHERE kind='recommendations' AND status='complete' ORDER BY created_at DESC LIMIT 1")).scalar()
        raw = conn.execute(text('SELECT result_json FROM ai_jobs WHERE id=:id'), {'id': job_id}).scalar() if job_id else None
        cache = json.loads(raw) if isinstance(raw, str) else raw or {}
    start = time.monotonic()
    result = app.build_sql_recommendations(20)
    elapsed = time.monotonic() - start
    items = result['items']
    snapshot = {
        'stage': args.stage, 'corpus': corpus, 'favorites': favorites,
        'favorite_signature': hashlib.sha256(json.dumps(favorites, sort_keys=True).encode()).hexdigest(),
        'seconds': round(elapsed, 3), 'result': result, 'previous_ai_cache': cache,
        'summary': {'items': len(items), 'sources': len(result['groups']),
                    'overlap_lte2': sum(r.get('keyword_overlap', 0) <= 2 for r in items),
                    'allocation': {g['name']: g['count'] for g in result['groups']}},
    }
    if args.stage == 'baseline':
        snapshot['cached_display'] = select_mode(cache.get('items', []), 'match', 20)
    else:
        baseline = json.loads((output / 'baseline.json').read_text(encoding='utf-8'))
        snapshot['same_inputs'] = (baseline['favorite_signature'] == snapshot['favorite_signature']
                                   and baseline['corpus'] == corpus)
        snapshot['summary']['overlap_with_baseline'] = len(
            {str(r['article_number']) for r in items} &
            {str(r['article_number']) for r in baseline['result']['items']})
    (output / f'{args.stage}.json').write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({k: v for k, v in snapshot.items() if k in {'stage', 'summary', 'seconds', 'same_inputs'}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
