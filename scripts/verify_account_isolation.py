"""Exercise real MySQL and HTTP handlers; roll back every verification write."""
import argparse
from contextlib import nullcontext
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sqlalchemy import text
from account_store import AccountStore, account_scope
from local_ai_recommendations import RecommendationService
from local_ai_repository import LocalAIRepository
import web.app as server


class BoundEngine:
    def __init__(self, connection):
        self.connection = connection

    def connect(self):
        return nullcontext(self.connection)

    def begin(self):
        return nullcontext(self.connection)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--member', required=True)
    args = parser.parse_args()
    original_engine = server.engine
    with original_engine.connect() as connection:
        transaction = connection.begin()
        try:
            source = dict(connection.execute(text('SELECT * FROM accounts WHERE username=:name'), {'name': args.source}).mappings().one())
            member = dict(connection.execute(text('SELECT * FROM accounts WHERE username=:name'), {'name': args.member}).mappings().one())
            state = lambda aid: set(connection.execute(text('SELECT paper_id FROM account_favorites WHERE account_id=:id'), {'id': aid}).scalars())
            before_source, before_member = state(source['id']), state(member['id'])
            legacy = set(connection.execute(text('SELECT id FROM papers WHERE is_favorite=1')).scalars())
            assert before_member == before_source == legacy, 'Initial copied favorite lists differ.'
            connection.execute(text('UPDATE accounts SET is_active=1 WHERE id=:id'), {'id': member['id']})
            favorite = dict(connection.execute(text('SELECT id,article_number,citation_count FROM papers WHERE is_favorite=1 LIMIT 1')).mappings().one())
            other = dict(connection.execute(text('SELECT id,article_number FROM papers WHERE is_favorite=0 LIMIT 1')).mappings().one())
            bound = BoundEngine(connection)
            with patch.object(server, 'engine', bound), patch.object(server, 'accounts', AccountStore(bound)), patch.dict(server.app.config, TESTING=True, ACCOUNT_AUTH_ENABLED=True):
                def client(account):
                    result = server.app.test_client()
                    with result.session_transaction() as session:
                        session.update(authenticated=True, account_id=account['id'], username=account['username'],
                            role=account['role'], session_version=account['session_version'], csrf_token='verification')
                    return result
                admin_client, member_client = client(source), client(member)
                def post(client, path, body):
                    response = client.post(path, json=body, headers={'X-CSRF-Token': 'verification'})
                    assert response.status_code == 200, (path, response.status_code)
                    return response.json
                post(member_client, '/api/favorite', {'article_number': favorite['article_number'], 'favorite': False, 'account_id': source['id']})
                assert state(source['id']) == before_source
                assert state(member['id']) == before_member - {favorite['id']}
                assert member_client.get('/api/meta').json['fav_total'] == len(before_member) - 1
                assert admin_client.get('/api/meta').json['fav_total'] == len(before_source)
                post(member_client, '/api/favorites/bulk', {'article_numbers': [other['article_number']]})
                post(member_client, '/api/recommendations/feedback', {'article_number': other['article_number'], 'action': 'dismissed'})
                admin_feedback = admin_client.get('/api/recommendations/feedback').json['items']
                assert not any(row['article_number'] == other['article_number'] for row in admin_feedback)
                for client_instance in (admin_client, member_client):
                    for path in ('/', '/serdes', '/ai', '/account/password', '/api/papers?fav_only=1', '/api/serdes/meta', '/api/serdes/papers?size=2'):
                        response = client_instance.get(path)
                        assert response.status_code == 200, (path, response.status_code)
                with account_scope(member['id']):
                    repo = LocalAIRepository(bound)
                    assert repo.get_papers([favorite['article_number']])[0]['is_favorite'] == 0
                    repo.search('', 'serdes', {}, limit=2)
                    repo.review_queue(limit=2)
                    reco = RecommendationService(bound, None, server.build_sql_recommendation_pool, account_id=member['id'])
                    snapshot = reco.snapshot()
                    assert len(snapshot['favorites']) == len(before_member)
                    pool = server.build_sql_recommendations(3)
                    assert not any(p['article_number'] == other['article_number'] for p in pool['items'])
                    reco.enrich(pool['items'][:2])
                    reco.close()
                post(admin_client, '/api/favorite', {'article_number': favorite['article_number'], 'favorite': False})
                post(admin_client, '/api/favorite', {'article_number': favorite['article_number'], 'favorite': True})
                assert state(member['id']) == (before_member - {favorite['id']}) | {other['id']}
                # Existing maintenance scripts propagate only to the original owner.
                connection.execute(text('UPDATE papers SET is_favorite=1 WHERE id=:id'), {'id': other['id']})
                assert other['id'] in state(source['id'])
                connection.execute(text('UPDATE papers SET is_favorite=0 WHERE id=:id'), {'id': other['id']})
                assert other['id'] not in state(source['id'])
                assert other['id'] in state(member['id'])
                assert connection.execute(text('SELECT citation_count FROM papers WHERE id=:id'), {'id': favorite['id']}).scalar() == favorite['citation_count']
                # Verify inserted/deleted AI jobs with account ownership on actual MySQL.
                with account_scope(source['id']):
                    job = LocalAIRepository(bound).create_job({'owner': source['username'], 'kind': 'ask', 'model': 'verification', 'request': {}})
                with account_scope(member['id']):
                    assert LocalAIRepository(bound).get_job(job, source['username']) is None
        finally:
            transaction.rollback()
    with original_engine.connect() as connection:
        for account, expected in ((source, before_source), (member, before_member)):
            actual = set(connection.execute(text('SELECT paper_id FROM account_favorites WHERE account_id=:id'), {'id': account['id']}).scalars())
            assert actual == expected
        assert set(connection.execute(text('SELECT id FROM papers WHERE is_favorite=1')).scalars()) == legacy
        active = connection.execute(text('SELECT is_active FROM accounts WHERE id=:id'), {'id': member['id']}).scalar()
    print(json.dumps(dict(mysql_isolation_passed=True, copied_favorites=len(before_member),
                          verification_writes_rolled_back=True, member_password_configured=bool(active)), indent=2))


if __name__ == '__main__':
    main()
