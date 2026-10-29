"""SQL-backed login, presence, root authorization and session lifecycle tests."""
from datetime import datetime, timedelta
import json
import unittest
from unittest.mock import patch

from sqlalchemy import select, text

from paper_server.account_activity import ActivityStore, _recent, login_sessions, metadata
from tests import test_account_isolation as fixtures
import web.app as server


class AccountActivityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.AccountIsolationTests.setUpClass()

    def setUp(self):
        self.fixture = fixtures.AccountIsolationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.engine = self.fixture.engine
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE accounts SET username='admin' WHERE id=1"))
        metadata.create_all(self.engine)
        self.config = patch.dict(server.app.config, ACCOUNT_ACTIVITY_ENABLED=True)
        self.config.start()
        self.addCleanup(self.config.stop)
        _recent.clear()
        self.store = ActivityStore(self.engine)

    def login(self, username='admin'):
        client = server.app.test_client()
        client.get('/login')
        with client.session_transaction() as session:
            csrf = session['csrf_token']
        response = client.post('/login',data=dict(username=username,
            password=self.fixture.password,csrf_token=csrf))
        self.assertEqual(response.status_code,302)
        return client

    def rows(self):
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(select(login_sessions)).mappings()]

    def test_successful_login_and_logout_are_recorded_without_raw_tokens(self):
        client = self.login()
        rows = self.rows()
        self.assertEqual(len(rows),1)
        self.assertIsNotNone(rows[0]['login_at'])
        self.assertEqual(rows[0]['account_id'],1)
        self.assertEqual(rows[0]['expires_at']-rows[0]['last_seen_at'],timedelta(hours=12))
        with client.session_transaction() as session:
            self.assertNotEqual(rows[0]['session_key'],session['activity_session_id'])
            self.assertNotEqual(rows[0]['session_key'],session['csrf_token'])
            token = session['csrf_token']
        response = client.post('/logout',headers={'X-CSRF-Token':token})
        self.assertEqual(response.status_code,302)
        row = self.rows()[0]
        self.assertEqual(row['end_reason'],'logout')
        self.assertIsNotNone(row['ended_at'])
        account = self.store.overview()['accounts'][0]
        self.assertEqual(account['active_sessions'],0)
        self.assertEqual(account['status'],'offline')

    def test_failed_login_is_not_a_successful_session(self):
        client = server.app.test_client()
        client.get('/login')
        with client.session_transaction() as session:
            token=session['csrf_token']
        response=client.post('/login',data=dict(username='admin',password='wrong-password',csrf_token=token))
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.rows(),[])

    def test_password_change_keeps_the_current_session_active_and_revokes_old_versions(self):
        client=self.login()
        self.store.touch('d'*64,1,1,timedelta(hours=12),login=True)
        with client.session_transaction() as session:
            token=session['csrf_token']
        response=client.post('/account/password',data=dict(current_password=self.fixture.password,
            new_password='A-new-private-password-52!',confirmation='A-new-private-password-52!',csrf_token=token))
        self.assertEqual(response.status_code,302)
        overview=self.store.overview()
        root=next(item for item in overview['accounts'] if item['id']==1)
        self.assertEqual(root['active_sessions'],1)
        self.assertIn('revoked',[row['status'] for row in overview['recent_sessions']])
        self.assertEqual(client.get('/account/activity').status_code,200)

    def test_existing_sessions_are_observed_without_inventing_login_time(self):
        self.fixture.member.get('/account/password')
        self.fixture.member.get('/account/password')
        rows=self.rows()
        self.assertEqual(len(rows),1)
        self.assertIsNone(rows[0]['login_at'])
        result=self.store.overview()
        account=next(item for item in result['accounts'] if item['id']==2)
        self.assertEqual(account['status'],'online')
        self.assertIsNone(account['last_login_at'])
        self.assertEqual(result['recent_sessions'][0]['kind'],'observed')

    def test_presence_windows_expiration_password_version_and_multiple_devices(self):
        now=datetime(2026,10,28,6,0,0)
        self.store.touch('a'*64,2,1,timedelta(hours=12),login=True,now=now-timedelta(seconds=301))
        self.store.touch('b'*64,2,1,timedelta(hours=12),login=True,now=now-timedelta(seconds=300))
        result=self.store.overview(now=now)
        account=next(item for item in result['accounts'] if item['id']==2)
        self.assertEqual((account['active_sessions'],account['online_sessions']),(2,1))
        self.assertEqual(account['status'],'online')
        self.store.logout('b'*64,2,now=now)
        account=next(item for item in self.store.overview(now=now)['accounts'] if item['id']==2)
        self.assertEqual(account['status'],'idle')
        with self.engine.begin() as conn:
            conn.execute(text('UPDATE accounts SET session_version=2 WHERE id=2'))
        result=self.store.overview(now=now)
        account=next(item for item in result['accounts'] if item['id']==2)
        self.assertEqual(account['active_sessions'],0)
        self.assertIn('revoked',[item['status'] for item in result['recent_sessions']])
        self.store.touch('c'*64,2,2,timedelta(seconds=0),now=now)
        result=self.store.overview(now=now)
        self.assertEqual(next(item for item in result['accounts'] if item['id']==2)['active_sessions'],0)
        self.assertIn('expired',[item['status'] for item in result['recent_sessions']])

    def test_unknown_and_disabled_accounts_are_distinct(self):
        accounts=self.store.overview()['accounts']
        self.assertEqual(next(item for item in accounts if item['id']==3)['status'],'unknown')
        self.assertEqual(next(item for item in accounts if item['id']==4)['status'],'disabled')

    def test_root_only_even_if_another_account_is_admin_or_forges_username(self):
        anonymous=server.app.test_client()
        self.assertEqual(anonymous.get('/api/account/activity').status_code,401)
        self.assertEqual(anonymous.get('/account/activity').status_code,302)
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE accounts SET role='admin' WHERE id=2"))
        with self.fixture.member.session_transaction() as session:
            session['username']='admin'
        for client in (self.fixture.member,self.fixture.reader):
            self.assertEqual(client.get('/api/account/activity?account_id=1&username=admin').status_code,403)
            self.assertEqual(client.get('/account/activity').status_code,403)
        root=self.fixture.admin.get('/api/account/activity')
        self.assertEqual(root.status_code,200)
        serialized=json.dumps(root.json)
        for forbidden in ('session_key','activity_session_id','csrf_token','password_hash','current_version'):
            self.assertNotIn(forbidden,serialized)
        self.assertIn('no-store',root.headers['Cache-Control'])
        self.assertEqual(self.fixture.admin.get('/account/activity').status_code,200)
        self.assertIn(b'href="/account/activity"',self.fixture.admin.get('/').data)
        self.assertNotIn(b'href="/account/activity"',self.fixture.reader.get('/').data)

    def test_heartbeat_requires_authentication_and_csrf(self):
        self.assertEqual(server.app.test_client().post('/api/account/heartbeat').status_code,401)
        self.assertEqual(self.fixture.member.post('/api/account/heartbeat').status_code,403)
        response=self.fixture.member.post('/api/account/heartbeat',headers={'X-CSRF-Token':'test-csrf'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.rows()[0]['account_id'],2)

    def test_presence_writes_are_throttled_and_storage_failure_does_not_break_pages(self):
        self.fixture.member.get('/account/password')
        initial=self.rows()[0]['last_seen_at']
        with patch('paper_server.account_activity.utcnow',return_value=initial+timedelta(seconds=10)):
            self.fixture.member.get('/account/password')
        self.assertEqual(self.rows()[0]['last_seen_at'],initial)
        with self.engine.begin() as conn:
            conn.execute(text('DROP TABLE account_login_sessions'))
        _recent.clear()
        with self.assertLogs(server.app.logger,level='WARNING'):
            response=self.fixture.member.get('/account/password')
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.fixture.admin.get('/api/account/activity').status_code,503)

    def test_shared_pages_include_presence_without_including_login_page(self):
        for path in ('/','/serdes','/connection','/daily-review','/account/password','/ai'):
            response=self.fixture.admin.get(path)
            self.assertEqual(response.status_code,200,path)
            self.assertIn(b'account_presence.js',response.data,path)
        self.assertNotIn(b'account_presence.js',server.app.test_client().get('/login').data)


if __name__=='__main__':
    unittest.main()
