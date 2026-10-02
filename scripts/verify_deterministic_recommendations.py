"""Verify deployed SQL-only favorite recommendations without changing source data."""
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
from recommendation_policy import TOTAL_RECOMMENDATIONS, excluded_electrical_multicarrier
from scripts.apply_recommendation_migration import snapshot

BASE = 'http://127.0.0.1:5001'
MODES = ('match', 'explore', 'recent', 'diverse', 'survey')


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
    assert 'id="btn-reco-ai"' not in page.text
    with engine.connect() as conn:
        before = snapshot(conn)
        jobs_before = conn.execute(text("SELECT COUNT(*) FROM ai_jobs WHERE kind='recommendations'")).scalar()

    views = {}
    for attempt in range(90):
        views = {mode: get('/api/recommendations?per_source=20&mode=' + mode) for mode in MODES}
        if all(view.get('refresh', {}).get('status') == 'ready' for view in views.values()):
            break
        time.sleep(2)
    else:
        raise RuntimeError('Deterministic recommendation pool did not become ready.')

    identifiers = {}
    modes = {}
    for mode, view in views.items():
        items = view['items']
        ids = [str(item['article_number']) for item in items]
        identifiers[mode] = ids
        assert view['engine'] == 'sql'
        assert view['recommendation_strategy'] == 'deterministic-sql-1'
        assert view['ai']['status'] == 'disabled' and not view['ai']['mode_fallback']
        assert len(ids) == len(set(ids)) and len(ids) <= TOTAL_RECOMMENDATIONS
        assert all(group['count'] <= 20 for group in view['groups'])
        assert all(item['recommendation_basis'] == 'deterministic' and not item['is_favorite']
                   and item.get('recommendation_reason') and item.get('interest_topic') for item in items)
        assert not any(excluded_electrical_multicarrier(item) for item in items)
        modes[mode] = {'items': len(ids), 'sources': len(view['groups'])}

    match = set(identifiers['match'])
    overlaps = {mode: len(match & set(ids)) for mode, ids in identifiers.items()}
    for mode in ('explore', 'recent', 'diverse'):
        assert set(identifiers[mode]) != match, f'{mode} did not change membership.'
    disabled = client.post(BASE + '/api/recommendations/refresh',
                           headers={'X-CSRF-Token': csrf}, timeout=30)
    assert disabled.status_code == 410 and disabled.json()['status'] == 'disabled'

    with engine.connect() as conn:
        after = snapshot(conn)
        jobs_after = conn.execute(text("SELECT COUNT(*) FROM ai_jobs WHERE kind='recommendations'")).scalar()
    assert before == after and jobs_before == jobs_after
    report = {'verified_at': datetime.now().isoformat(timespec='seconds'), 'modes': modes,
              'overlap_with_match': overlaps, 'source_before': before, 'source_after': after,
              'source_unchanged': True, 'recommendation_jobs_unchanged': jobs_before == jobs_after}
    target = ROOT / 'outputs/recommendation_quality/deterministic_deployment.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
