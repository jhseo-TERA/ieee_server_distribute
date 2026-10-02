"""Real SQL + HTTP boundary tests using an isolated in-memory database."""
import threading
import time
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool
from werkzeug.security import generate_password_hash

import web.app as server
from account_store import AccountStore, account_scope, current_account_id
from local_ai_recommendations import RecommendationService
from local_ai_repository import LocalAIRepository
from recommendation_feedback import FeedbackStore


class AccountIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = 'Private-test-password-42!'
        cls.hashed = generate_password_hash(cls.password)

    def setUp(self):
        self.engine = create_engine('sqlite://', poolclass=StaticPool,
                                    connect_args={'check_same_thread': False})
        @event.listens_for(self.engine, 'before_cursor_execute', retval=True)
        def sqlite_lock_syntax(conn, cursor, statement, parameters, context, executemany):
            return statement.replace(' FOR UPDATE', ''), parameters
        with self.engine.begin() as conn:
            for ddl in [
                'CREATE TABLE accounts (id INTEGER PRIMARY KEY,username TEXT UNIQUE,password_hash TEXT,'
                'role TEXT,is_active INT,must_change_password INT DEFAULT 0,session_version INT DEFAULT 1,legacy_owner INT DEFAULT 0)',
                'CREATE TABLE papers (id INTEGER PRIMARY KEY,article_number TEXT UNIQUE,title TEXT,authors TEXT,'
                'year TEXT,source_name TEXT,source_type TEXT,source_system TEXT,issue TEXT,url TEXT,pdf_available INT,'
                'is_favorite INT,citation_count INT,citation_source TEXT,citation_updated_at TEXT)',
                'CREATE TABLE account_favorites (account_id INT,paper_id INT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,'
                'PRIMARY KEY(account_id,paper_id))',
                'CREATE TABLE account_recommendation_feedback (account_id INT,article_number TEXT,action TEXT,'
                'updated_at TEXT DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(account_id,article_number))',
                'CREATE TABLE ai_jobs (id TEXT PRIMARY KEY,owner TEXT,account_id INT,kind TEXT,model TEXT,status TEXT,'
                'request_json TEXT,result_json TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,updated_at TEXT DEFAULT CURRENT_TIMESTAMP)',
            ]:
                conn.execute(text(ddl))
            for i, name, role, active in [(1, 'admin', 'admin', 1), (2, 'member', 'member', 1),
                                          (3, 'reader', 'viewer', 1), (4, 'pending', 'member', 0)]:
                conn.execute(text('INSERT INTO accounts(id,username,password_hash,role,is_active,legacy_owner) '
                    'VALUES (:id,:name,:hashed,:role,:active,:legacy)'),
                    dict(id=i, name=name, hashed=self.hashed, role=role, active=active, legacy=int(i == 1)))
            for i in (1, 2, 3):
                conn.execute(text('INSERT INTO papers(id,article_number,title,year,source_name,source_type,source_system,'
                    "is_favorite,citation_count,pdf_available) VALUES (:id,:article,:title,'2025','JSSC','journal','ieee',:fav,99,1)"),
                    dict(id=i, article=str(i), title=f'Paper {i}', fav=int(i == 1)))
            conn.execute(text('INSERT INTO account_favorites(account_id,paper_id) VALUES (1,1),(2,1),(3,1)'))
        self.store = AccountStore(self.engine)
        self.config = patch.dict(server.app.config, ACCOUNT_AUTH_ENABLED=True, TESTING=True, SESSION_COOKIE_SECURE=False)
        self.patches = [self.config, patch.object(server, 'engine', self.engine),
                        patch.object(server, 'accounts', self.store), patch.object(server, '_account_recommendations', {})]
        for item in self.patches:
            item.start()
        server._login_failures.clear()
        self.admin = self.client(1, 'admin', 'admin')
        self.member = self.client(2, 'member', 'member')
        self.reader = self.client(3, 'reader', 'viewer')

    def tearDown(self):
        for service in server._account_recommendations.values():
            service.close()
        for item in reversed(self.patches):
            item.stop()
        self.engine.dispose()

    def client(self, account_id, username, role):
        client = server.app.test_client()
        with client.session_transaction() as session:
            session.update(authenticated=True, account_id=account_id, username=username, role=role,
                           session_version=1, csrf_token='test-csrf')
        return client

    def post(self, client, path, data):
        return client.post(path, json=data, headers={'X-CSRF-Token': 'test-csrf'})

    def favorites(self, account_id):
        with self.engine.connect() as conn:
            return set(conn.execute(text('SELECT paper_id FROM account_favorites WHERE account_id=:id'),
                                    {'id': account_id}).scalars())

    def test_member_edit_is_private_even_with_forged_owner(self):
        result = self.post(self.member, '/api/favorite', {'article_number': '1', 'favorite': False, 'account_id': 1})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.favorites(1), {1})
        self.assertEqual(self.favorites(2), set())
        with self.engine.connect() as conn:
            self.assertEqual(tuple(conn.execute(text('SELECT is_favorite,citation_count FROM papers WHERE id=1')).one()), (1, 99))
        self.assertEqual(self.member.get('/api/meta').json['fav_total'], 0)
        self.assertEqual(self.admin.get('/api/meta').json['fav_total'], 1)
        self.assertEqual(self.member.get('/api/papers?fav_only=1').json['total'], 0)
        self.assertEqual(self.admin.get('/api/papers?fav_only=1').json['items'][0]['is_favorite'], 1)
        self.assertIsNone(current_account_id.get())

    def test_pdf_favorite_filter_combinations_and_summary(self):
        with self.engine.begin() as conn:
            conn.execute(text('UPDATE papers SET pdf_available=0 WHERE id=1'))
            conn.execute(text('DELETE FROM account_favorites WHERE account_id=2'))
            conn.execute(text('INSERT INTO account_favorites(account_id,paper_id) VALUES (2,2)'))
        for client, favorites in ((self.admin, {'1'}), (self.member, {'2'})):
            for pdf in ('', 'present', 'missing'):
                for fav in ('', 'favorite', 'nonfavorite'):
                    with self.subTest(account=favorites, pdf=pdf, fav=fav):
                        expected = {'1', '2', '3'}
                        if pdf:
                            expected &= {'2', '3'} if pdf == 'present' else {'1'}
                        if fav:
                            expected &= favorites if fav == 'favorite' else {'1', '2', '3'} - favorites
                        result = client.get(f'/api/papers?pdf_status={pdf}&fav_status={fav}').json
                        self.assertEqual({item['article_number'] for item in result['items']}, expected)
                        self.assertEqual(result['total'], len(expected))
            meta = client.get('/api/meta').json
            self.assertEqual(meta['fav_total'], meta['fav_pdf_total'] + meta['fav_missing_pdf_total'])
            self.assertEqual(meta['pdf_total'], meta['fav_pdf_total'] + meta['nonfav_pdf_total'])
            self.assertEqual(meta['fav_missing_pdf_total'], int('1' in favorites))
            self.assertEqual(meta['nonfav_pdf_total'], 2 - int('2' in favorites))
        self.assertEqual(self.admin.get('/api/papers?pdf_only=1').json['total'], 2)
        self.assertEqual(self.admin.get('/api/papers?pdf_status=invalid').status_code, 400)
        self.assertEqual(self.admin.get('/api/papers?fav_status=invalid').status_code, 400)

    def test_bulk_add_and_failed_batch_are_atomic(self):
        result = self.post(self.member, '/api/favorites/bulk', {'article_numbers': ['1', '2', '2']})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['changed_count'], 1)
        self.assertEqual(self.favorites(2), {1, 2})
        result = self.post(self.member, '/api/favorites/bulk', {'article_numbers': ['3', 'missing']})
        self.assertEqual(result.status_code, 404)
        self.assertEqual(self.favorites(2), {1, 2})
        self.assertEqual(self.favorites(1), {1})

    def test_admin_edits_only_own_state_and_keeps_shared_citations(self):
        self.post(self.admin, '/api/favorite', {'article_number': '1', 'favorite': False})
        self.assertEqual(self.favorites(1), set())
        self.assertEqual(self.favorites(2), {1})
        with self.engine.connect() as conn:
            self.assertEqual(tuple(conn.execute(text('SELECT is_favorite,citation_count FROM papers WHERE id=1')).one()), (0, 99))

    def test_feedback_is_private_and_restore_does_not_affect_other_user(self):
        for client in (self.member, self.admin):
            self.assertEqual(self.post(client, '/api/recommendations/feedback',
                {'article_number': '2', 'action': 'dismissed'}).status_code, 200)
        self.post(self.member, '/api/recommendations/feedback', {'article_number': '2', 'action': 'restore', 'account_id': 1})
        self.assertEqual(self.member.get('/api/recommendations/feedback').json['items'], [])
        self.assertEqual(len(self.admin.get('/api/recommendations/feedback').json['items']), 1)

    def test_reader_and_csrf_protection_and_shared_write_permissions(self):
        self.assertEqual(self.post(self.reader, '/api/favorite', {'article_number': '1'}).status_code, 403)
        self.assertEqual(self.member.post('/api/favorite', json={'article_number': '1'}).status_code, 403)
        self.assertEqual(self.post(self.member, '/api/ai/proposals/decision', {'ids': [1], 'decision': 'approve'}).status_code, 403)
        response = self.member.get('/')
        self.assertIn(b'const CAN_EDIT_FAVORITES = true', response.data)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_missing_disabled_and_stale_sessions_fail_closed(self):
        old = server.app.test_client()
        with old.session_transaction() as session:
            session.update(authenticated=True, username='admin', role='admin')
        self.assertEqual(old.get('/api/meta').status_code, 401)
        with self.engine.begin() as conn:
            conn.execute(text('UPDATE accounts SET is_active=0 WHERE id=2'))
        self.assertEqual(self.member.get('/api/meta').status_code, 401)
        with self.engine.begin() as conn:
            conn.execute(text('UPDATE accounts SET session_version=2 WHERE id=1'))
        self.assertEqual(self.admin.get('/api/meta').status_code, 401)

    def test_database_role_overrides_stale_admin_cookie(self):
        forged = self.client(2, 'member', 'admin')
        self.assertEqual(self.post(forged, '/api/ai/proposals/decision', {'ids': [1], 'decision': 'approve'}).status_code, 403)

    def test_actual_login_and_pending_password_activation(self):
        client = server.app.test_client()
        with client.session_transaction() as session:
            session['csrf_token'] = 'login-token'
        response = client.post('/login', data={'username': 'member', 'password': self.password, 'csrf_token': 'login-token'})
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as session:
            self.assertEqual(session['account_id'], 2)
        self.assertIsNone(self.store.authenticate('pending', self.password))
        with self.assertRaisesRegex(ValueError, '8~256'):
            self.store.set_password('pending', 'Valid7!')
        self.assertIsNone(self.store.authenticate('pending', self.password))
        self.store.set_password('pending', 'Valid8!x')
        self.assertEqual(self.store.authenticate('pending', 'Valid8!x')['id'], 4)

    def test_password_change_invalidates_other_sessions(self):
        stale = self.client(2, 'member', 'member')
        self.assertIn(b'minlength="8"', self.member.get('/account/password').data)
        response = self.member.post('/account/password', data={
            'current_password': self.password, 'new_password': 'Change8!',
            'confirmation': 'Change8!', 'csrf_token': 'test-csrf'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.member.get('/api/meta').status_code, 200)
        self.assertEqual(stale.get('/api/meta').status_code, 401)
        self.assertIsNone(self.store.authenticate('member', self.password))

    def test_job_reads_and_creation_use_account_id(self):
        repo = LocalAIRepository(self.engine)
        with account_scope(1):
            job = repo.create_job({'owner': 'admin', 'kind': 'ask', 'model': 'test', 'request': {}})
        with account_scope(2):
            self.assertIsNone(repo.get_job(job, 'admin'))
            self.assertEqual(repo.list_jobs('admin'), [])
        with account_scope(1):
            self.assertEqual(repo.get_job(job, 'renamed-admin')['account_id'], 1)

    def test_recommendation_snapshot_and_background_caches_are_separate(self):
        self.store.set_favorites(2, ['2'], True)
        ready = threading.Event()
        seen = []
        def build(per_source):
            seen.append(current_account_id.get())
            ready.set()
            return {'items': [{'id': current_account_id.get()}]}
        services = [RecommendationService(self.engine, None, build, account_id=i) for i in (1, 2)]
        try:
            for i, service in enumerate(services, 1):
                with account_scope(i):
                    snapshot = service.snapshot()
                    self.assertEqual(len(snapshot['favorites']), i)
                    service.sql_cache.debounce = 0
                    ready.clear()
                    value, _ = service.sql_cache.get('same-key', 20)
                    self.assertIsNone(value)
                    self.assertTrue(ready.wait(2))
                deadline = time.monotonic() + 2
                while not service.sql_cache.entries and time.monotonic() < deadline:
                    time.sleep(.01)
            self.assertEqual(seen, [1, 2])
            for i, service in enumerate(services, 1):
                value, _ = service.sql_cache.get('same-key', 20)
                self.assertEqual(value['items'][0]['id'], i)
        finally:
            for service in services:
                service.close()


if __name__ == '__main__':
    unittest.main()
