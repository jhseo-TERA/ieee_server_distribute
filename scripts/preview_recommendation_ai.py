"""Exercise the real recommendation pipeline without publishing jobs or profiles."""
import json
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sqlalchemy import text
import web.app as app
from local_ai_runtime import OllamaClient
from local_ai_service import LocalAIService
from local_ai_recommendations import RecommendationService, MODEL, MODES, select_mode, TOTAL_RECOMMENDATIONS
from recommendation_pair_cache import MemoryPairCache


class PreviewWorker:
    _chat = LocalAIService._chat

    def __init__(self):
        self.runtime = OllamaClient(os.getenv('LOCAL_AI_OLLAMA_URL', 'http://127.0.0.1:11434'))

    def _progress(self, job_id, message):
        print(message, flush=True)


class PreviewService(RecommendationService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pair_cache = MemoryPairCache()

    def profile(self, snap, seeds, job_id, req, event):
        return super().profile(snap, seeds, job_id, req, event, persist=False)


def main():
    with app.engine.connect() as conn:
        active = conn.execute(text("SELECT COUNT(*) FROM ai_jobs WHERE status IN ('running','queued')")).scalar()
    if active:
        raise RuntimeError('A research job is active; rerun this preview after it completes.')
    service = PreviewService(app.engine, PreviewWorker(), app.build_sql_recommendations)
    start = time.monotonic()
    before = service.snapshot()
    try:
        result = service.run('local-preview', {'signature': before['signature'], 'model': MODEL}, threading.Event())
        after = service.snapshot()
        modes = {mode: select_mode(result['items'], mode, 20, ai_only=True) for mode in MODES}
        for items in modes.values():
            assert len(items) <= TOTAL_RECOMMENDATIONS
            assert len({p['article_number'] for p in items}) == len(items)
            assert all(p['ai_score'] >= 70 and not p['is_favorite'] for p in items)
        payload = {'seconds': round(time.monotonic() - start, 2), 'same_inputs': before['signature'] == after['signature'],
                   'result': result, 'modes': modes}
        output = ROOT / 'outputs' / 'recommendation_quality' / 'ai_preview.json'
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        print(json.dumps({'seconds': payload['seconds'], 'same_inputs': payload['same_inputs'],
                          'attempted': result['attempted_count'], 'evaluated': result['evaluated_count'],
                          'qualified': result['qualified_count'], 'failed_batches': result['failed_batches'],
                          'modes': {mode: len(items) for mode, items in modes.items()}}, ensure_ascii=False), flush=True)
    finally:
        service.close()


if __name__ == '__main__':
    main()
