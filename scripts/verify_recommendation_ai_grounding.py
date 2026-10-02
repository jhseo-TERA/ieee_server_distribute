"""Targeted real-model regression: prior passing papers plus nearest candidates."""
import json
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.preview_recommendation_ai import PreviewService, PreviewWorker
from local_ai_recommendations import MODEL, select_mode
from recommendation_policy import allocate
import web.app as app


def main():
    output = ROOT / 'outputs/recommendation_quality'
    previous = json.loads((output/'ai_preview.json').read_text(encoding='utf-8'))
    base = app.build_sql_recommendations(30, candidate_pool=True)
    prior_ids = {p['article_number'] for p in previous['result']['items']}
    priority = [p for p in base['items'] if p['article_number'] in prior_ids]
    for row in allocate(base['items'], total=64, per_source=20):
        if row['article_number'] not in {p['article_number'] for p in priority}:
            priority.append(row)
        if len(priority) >= 16:
            break
    sample = {**base, 'items': priority}
    service = PreviewService(app.engine, PreviewWorker(), lambda *args, **kwargs: sample)
    # Reuse the generated profile for a controlled grounding-only comparison.
    service.profile = lambda *args: previous['result']['profile']
    start = time.monotonic()
    before = service.snapshot()
    try:
        with patch('local_ai_recommendations.MAX_CANDIDATES', 16):
            snap = service.snapshot()
            result = service.run('grounding-preview', {'signature':snap['signature'], 'model':MODEL}, threading.Event())
        after = service.snapshot()
        assert before['signature'] == after['signature']
        items = select_mode(result['items'], 'match', 20, ai_only=True)
        assert all(p['ai_favorite_id'] == p['related_favorite_id'] for p in items)
        payload = {'seconds':round(time.monotonic()-start, 2), 'same_inputs':True,
                   'sampling':'All 10 previously qualified papers + 6 high-similarity candidates; not a full 64-paper rerun.',
                   'result':result, 'items':items}
        (output/'ai_grounding_regression.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
        print(json.dumps({'seconds':payload['seconds'], 'attempted':result['attempted_count'],
                          'evaluated':result['evaluated_count'], 'qualified':len(items),
                          'nearest_favorite_verified':True},ensure_ascii=False),flush=True)
    finally:
        service.close()


if __name__ == '__main__':
    main()
