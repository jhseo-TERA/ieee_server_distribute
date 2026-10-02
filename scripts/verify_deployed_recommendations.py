"""Verify the running loopback server and its real recommendation refresh.

Use the workspace owner's local signing configuration only in memory. Never log
credentials or the short-lived local maintenance session cookie.
"""
from datetime import datetime
import json
from pathlib import Path
import secrets
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import requests
from web.app import app, ADMIN_USERNAME, AUTH_ACCOUNTS, engine
from recommendation_policy import VERSION, TOTAL_RECOMMENDATIONS, excluded_electrical_multicarrier
from scripts.apply_recommendation_migration import snapshot

BASE = 'http://127.0.0.1:5001'


def main():
    if AUTH_ACCOUNTS.get(ADMIN_USERNAME, {}).get('role') != 'admin':
        raise RuntimeError('Configured administrator is missing.')
    client = requests.Session()
    client.trust_env = False
    csrf = secrets.token_urlsafe(32)
    signed = app.session_interface.get_signing_serializer(app).dumps({
        'authenticated':True,'username':ADMIN_USERNAME,'role':'admin','csrf_token':csrf})
    client.cookies.set(app.config['SESSION_COOKIE_NAME'], signed, domain='127.0.0.1', path='/')

    def get(path):
        response = client.get(BASE+path, timeout=30, allow_redirects=False)
        response.raise_for_status()
        return response.json()

    assert get('/health')['ok']
    feedback = get('/api/recommendations/feedback')
    assert feedback['available']
    page = client.get(BASE+'/', timeout=30)
    page.raise_for_status()
    assert 'saveRecoFeedback' in page.text and '추천에서 제외한 논문' in page.text
    initial = get('/api/recommendations?per_source=20')
    assert initial['ai']['version'] == VERSION
    assert initial['total_limit'] == 60 and initial['feedback_available']
    refresh = client.post(BASE+'/api/recommendations/refresh', headers={'X-CSRF-Token':csrf}, timeout=30)
    refresh.raise_for_status()
    print(json.dumps({'health':True, 'policy':VERSION, 'feedback_available':True,
                      'refresh':refresh.json()},ensure_ascii=False),flush=True)
    started = time.monotonic()
    previous = None
    view = initial
    while time.monotonic()-started < 1800:
        view = get('/api/recommendations?per_source=20')
        ai = view['ai']
        assert len(view['items']) <= TOTAL_RECOMMENDATIONS
        assert not any(excluded_electrical_multicarrier(p) for p in view['items'])
        progress = (ai['status'], ai.get('progress'), view['engine'], len(view['items']))
        if progress != previous:
            print(json.dumps({'seconds':round(time.monotonic()-started), 'status':ai['status'],
                              'progress':ai.get('progress'), 'engine':view['engine'], 'items':len(view['items'])},ensure_ascii=False),flush=True)
            previous = progress
        if view['engine']=='hybrid' and not ai['stale'] and ai['version']==VERSION:
            break
        if ai.get('error'):
            raise RuntimeError(ai['error'])
        time.sleep(10)
    else:
        raise RuntimeError('Deployment refresh deadline exceeded; inspect the running job.')
    modes = {}
    for mode in ('match','explore','recent','survey','diverse'):
        current = get('/api/recommendations?per_source=20&mode='+mode)
        items = current['items']
        assert current['engine']=='hybrid' and not current['ai']['stale']
        assert len(items)<=TOTAL_RECOMMENDATIONS and all(g['count']<=20 for g in current['groups'])
        assert len({p['article_number'] for p in items})==len(items)
        assert all(p['recommendation_basis']=='ai' and p['ai_score']>=70 and not p['is_favorite'] for p in items)
        assert all(p['ai_favorite_id']==p['related_favorite_id'] for p in items)
        assert not any(excluded_electrical_multicarrier(p) for p in items)
        modes[mode]={'items':len(items), 'sources':len(current['groups'])}
    migration = json.loads((ROOT/'outputs/recommendation_quality/deployment_migration.json').read_text(encoding='utf-8'))
    with engine.connect() as conn:
        after = snapshot(conn)
    result = {'verified_at':datetime.now().isoformat(timespec='seconds'), 'policy':VERSION,
              'ai':view['ai'], 'modes':modes, 'feedback_available':True,
              'source_before':migration['source_before'], 'source_after':after,
              'source_unchanged':after==migration['source_before'], 'items':view['items']}
    target = ROOT/'outputs/recommendation_quality/deployment_verification.json'
    target.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='items'},ensure_ascii=False),flush=True)


if __name__ == '__main__':
    main()
