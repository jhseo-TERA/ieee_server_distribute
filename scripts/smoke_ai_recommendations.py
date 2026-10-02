"""Opt-in full local recommendation refresh; never changes favorites/measurements."""
from pathlib import Path
import json
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import text
from web.app import app, engine
from local_ai_recommendations import MODES


def counts():
    with engine.connect() as conn:
        return dict(conn.execute(text('SELECT COUNT(*) papers,SUM(is_favorite=1) favorites,'
                    '(SELECT COUNT(*) FROM serdes_measurements) measurements FROM papers')).mappings().one())


def main():
    ai = app.blueprints['local_ai'].ai_service_factory()
    reco = ai.recommendations
    before = counts()
    started = time.monotonic()
    job = reco.refresh(force=True)
    print(json.dumps(job), flush=True)
    job_id = job.get('job_id')
    if not job_id:
        raise RuntimeError('No refresh job was created')
    last_print = -20
    while time.monotonic() - started < 1800:
        current = ai.repo.get_job(job_id)
        elapsed = int(time.monotonic() - started)
        if elapsed - last_print >= 20:
            print(json.dumps({'seconds': elapsed, 'status': current['status'],
                              'progress': (current.get('result') or {}).get('progress', '')}, ensure_ascii=False), flush=True)
            last_print = elapsed
        if current['status'] in {'failed', 'cancelled'}:
            print(json.dumps({'status': current['status'], 'error': current.get('error')}, ensure_ascii=False), flush=True)
            return 1
        if current['status'] == 'complete':
            result = current['result']
            checked = {}
            for mode in MODES:
                view = reco.view(mode)
                keys = [p['article_number'] for p in view['items']]
                assert len(keys) == len(set(keys)), 'duplicate recommendations'
                assert not any(p['is_favorite'] for p in view['items']), 'favorite leaked into recommendations'
                assert all(g['count'] <= 15 for g in view['groups'])
                checked[mode] = {'items': len(keys), 'ai': sum(p['recommendation_basis'] == 'ai' for p in view['items'])}
            after = counts()
            assert before == after, 'source data counts changed'
            print(json.dumps({'status': 'complete', 'seconds': elapsed, 'source_counts_unchanged': True,
                              'profile': result['profile'], 'evaluated': result['evaluated_count'],
                              'candidates': result['candidate_count'], 'rejected': result['rejected_count'],
                              'failed_batches': result['failed_batches'], 'modes': checked,
                              'examples': [{k: p.get(k) for k in ['article_number', 'title', 'ai_score', 'ai_topic', 'ai_reason', 'ai_evidence']}
                                           for p in result['items'] if 'ai_score' in p][:3]}, ensure_ascii=False, default=str), flush=True)
            return 0
        time.sleep(1)
    ai.cancel(job_id, '__recommendation_worker__')
    raise RuntimeError('Recommendation smoke deadline exceeded')


if __name__ == '__main__':
    raise SystemExit(main())
