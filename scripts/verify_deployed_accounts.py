"""Read-only loopback acceptance. Never print session signing material/cookies."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import requests
from sqlalchemy import text
from web.app import app, engine


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--accounts', nargs='+', required=True)
    args = parser.parse_args()
    result = {'health': requests.get('http://127.0.0.1:5001/health', timeout=5).json()['ok'], 'accounts': []}
    for username in args.accounts:
        with engine.connect() as conn:
            account = dict(conn.execute(text('SELECT id,username,role,is_active,session_version FROM accounts '
                'WHERE username=:name'), {'name': username}).mappings().one())
            expected = conn.execute(text('SELECT COUNT(*) FROM account_favorites WHERE account_id=:id'),
                                    {'id': account['id']}).scalar()
        item = {'username': username, 'role': account['role'], 'favorites': expected,
                'active': bool(account['is_active'])}
        with requests.Session() as client:
            signed = app.session_interface.get_signing_serializer(app).dumps(dict(
                authenticated=True, account_id=account['id'], username=username, role=account['role'],
                session_version=account['session_version'], csrf_token='loopback-read-only'))
            client.cookies.set(app.config.get('SESSION_COOKIE_NAME', 'session'), signed)
            response = client.get('http://127.0.0.1:5001/api/meta', timeout=10)
            if not account['is_active']:
                assert response.status_code == 401
                item['status'] = 'awaiting_local_password'
            else:
                assert response.status_code == 200
                assert response.json()['fav_total'] == expected
                for path in ('/', '/account/password', '/api/papers?fav_only=1&size=2'):
                    response = client.get('http://127.0.0.1:5001' + path, timeout=10)
                    assert response.status_code == 200, (path, response.status_code)
                for _ in range(30):
                    response = client.get('http://127.0.0.1:5001/api/recommendations?per_source=3', timeout=15)
                    assert response.status_code == 200
                    data = response.json()
                    assert data['fav_count'] == expected
                    if data['refresh']['status'] != 'refreshing':
                        break
                    time.sleep(.5)
                assert data['refresh']['status'] == 'ready', data['refresh']
                assert all(not paper['is_favorite'] for paper in data['items'])
                item.update(status='verified', recommendations=len(data['items']))
        result['accounts'].append(item)
    # Validate citation maintenance selection without contacting any provider.
    from scripts.update_favorite_citations import db_connect, favorite_scope_sql, fetch_targets
    with db_connect() as conn:
        assert 'account_favorites' in favorite_scope_sql(conn)
        fetch_targets(conn, refresh_days=7, force=False, limit=1, source_system=None)
    result['citation_scope'] = 'all_account_favorites'
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
