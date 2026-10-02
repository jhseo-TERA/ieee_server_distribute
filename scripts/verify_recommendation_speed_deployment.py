"""Measure real HTTP refreshes without editing favorites or source metadata."""
from datetime import datetime
import json
from pathlib import Path
import secrets
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import requests
from sqlalchemy import text
from web.app import app, ADMIN_USERNAME, AUTH_ACCOUNTS, engine
from recommendation_policy import VERSION, TOTAL_RECOMMENDATIONS, excluded_electrical_multicarrier
from local_ai_recommendations import RANKING_MODE
from scripts.apply_recommendation_migration import snapshot

OUT = ROOT / 'outputs/recommendation_quality/speed_ab'
BASE = 'http://127.0.0.1:5001'


def main():
    assert AUTH_ACCOUNTS.get(ADMIN_USERNAME, {}).get('role') == 'admin'
    client = requests.Session()
    client.trust_env = False
    csrf = secrets.token_urlsafe(32)
    signed = app.session_interface.get_signing_serializer(app).dumps({
        'authenticated': True, 'username': ADMIN_USERNAME, 'role': 'admin', 'csrf_token': csrf})
    client.cookies.set(app.config['SESSION_COOKIE_NAME'], signed, domain='127.0.0.1', path='/')

    def get(path):
        response = client.get(BASE + path, timeout=30, allow_redirects=False)
        response.raise_for_status()
        return response.json()

    assert get('/health')['ok']
    page = client.get(BASE + '/', timeout=30)
    page.raise_for_status()
    assert 'new_attempted_count' in page.text and 'cached_count' in page.text
    with engine.connect() as conn:
        before = snapshot(conn)
    rounds = []
    labels = ('seeded_first_refresh', 'repeat_refresh') if RANKING_MODE == 'pair' else ('compact_refresh',)
    for label in labels:
        initial = get('/api/recommendations?per_source=20')
        assert initial['ai']['version'] == VERSION
        # The manual endpoint enforces its normal one-minute cooldown. Do not
        # edit jobs/timestamps to bypass it; exclude this wait from run timing.
        for _ in range(30):
            started = time.monotonic()
            response = client.post(BASE + '/api/recommendations/refresh',
                                   headers={'X-CSRF-Token': csrf}, timeout=30)
            response.raise_for_status()
            request = response.json()
            if request['status'] != 'cooldown':
                break
            time.sleep(3)
        else:
            raise RuntimeError('Manual refresh cooldown did not clear.')
        assert request['status'] in {'running', 'queued'}, request
        target = request['job_id']
        print(json.dumps({'round': label, 'refresh': request}, ensure_ascii=False), flush=True)
        previous = None
        while time.monotonic() - started < 1800:
            view = get('/api/recommendations?per_source=20')
            ai = view['ai']
            assert not any(excluded_electrical_multicarrier(p) for p in view['items'])
            if initial['engine'] == 'hybrid':
                assert view['engine'] == 'hybrid', 'A refresh oscillated back to SQL.'
            progress = (ai['job_id'], ai['status'], ai.get('progress'))
            if progress != previous:
                print(json.dumps({'round': label, 'seconds': round(time.monotonic() - started, 2),
                                  'status': ai['status'], 'progress': ai.get('progress')}, ensure_ascii=False), flush=True)
                previous = progress
            if (ai['job_id'] == target and ai['status'] == 'complete' and not ai['stale']
                    and ai.get('result_version') == VERSION and ai.get('ranking_mode') == RANKING_MODE):
                break
            if ai.get('error'):
                raise RuntimeError(ai['error'])
            time.sleep(2)
        else:
            raise RuntimeError('Refresh did not finish with the current signature.')
        with engine.connect() as conn:
            job = dict(conn.execute(text('SELECT created_at,updated_at,result_json FROM ai_jobs WHERE id=:id'),
                                    {'id': target}).mappings().one())
        payload = json.loads(job['result_json']) if isinstance(job['result_json'], str) else job['result_json']
        rounds.append({'label': label, 'job_id': target, 'observed_seconds': round(time.monotonic() - started, 3),
                       'job_seconds': round((job['updated_at'] - job['created_at']).total_seconds(), 3),
                       'ai': ai, 'usage': payload.get('usage'), 'retried_count': payload.get('retried_count', 0),
                       'timings': payload.get('timings')})
    modes = {}
    for mode in ('match', 'explore', 'recent', 'survey', 'diverse'):
        started = time.monotonic()
        current = get('/api/recommendations?per_source=20&mode=' + mode)
        items = current['items']
        assert current['engine'] == 'hybrid' and not current['ai']['stale']
        assert len(items) <= TOTAL_RECOMMENDATIONS and all(g['count'] <= 20 for g in current['groups'])
        assert len({p['article_number'] for p in items}) == len(items)
        assert all(p['recommendation_basis'] == 'ai' and p['ai_score'] >= 70 and not p['is_favorite'] for p in items)
        assert all(p['ai_favorite_id'] == p['related_favorite_id'] for p in items)
        assert not any(excluded_electrical_multicarrier(p) for p in items)
        modes[mode] = {'items': len(items), 'sources': len(current['groups']),
                       'api_seconds': round(time.monotonic() - started, 4)}
    with engine.connect() as conn:
        after = snapshot(conn)
    report = {'verified_at': datetime.now().isoformat(timespec='seconds'), 'version': VERSION,
              'rounds': rounds, 'modes': modes, 'source_before': before, 'source_after': after,
              'source_unchanged_during_verification': before == after}
    (OUT / 'deployment.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
