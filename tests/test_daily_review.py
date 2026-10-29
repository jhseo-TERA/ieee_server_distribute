"""Real SQL and HTTP coverage for daily reading and private notebook state."""
import unittest
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import web.app as server
from paper_server.accounts import AccountStore
from paper_server.daily_review import DailyReviewStore, metadata


class DailyReviewTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', poolclass=StaticPool,
                                    connect_args={'check_same_thread': False})
        with self.engine.begin() as conn:
            conn.execute(text('CREATE TABLE accounts (id INTEGER PRIMARY KEY,username TEXT,role TEXT,'
                'is_active INT,session_version INT,must_change_password INT,favorites_source_account_id INT)'))
            conn.execute(text('CREATE TABLE papers (id INTEGER PRIMARY KEY,article_number TEXT UNIQUE,'
                'title TEXT,authors TEXT,year TEXT,source_name TEXT,source_system TEXT,url TEXT,'
                'pdf_available INT,is_favorite INT)'))
            conn.execute(text('CREATE TABLE account_favorites (account_id INT,paper_id INT,'
                'PRIMARY KEY(account_id,paper_id))'))
            conn.execute(text("INSERT INTO accounts VALUES (1,'admin','admin',1,1,0,NULL),"
                "(2,'member','member',1,1,0,NULL),(3,'reader','viewer',1,1,0,1)"))
            for i in range(1, 6):
                conn.execute(text('INSERT INTO papers VALUES (:id,:article,:title,:authors,\'2026\','
                    "'JSSC','ieee','https://example.test/paper',1,:fav)"),
                    dict(id=i, article=str(i), title=f'Paper {i}', authors='A. Researcher', fav=int(i < 4)))
            conn.execute(text('INSERT INTO account_favorites VALUES (1,1),(1,2),(1,3),(2,4),(2,5)'))
        metadata.create_all(self.engine)
        self.patches = [patch.dict(server.app.config, TESTING=True, ACCOUNT_AUTH_ENABLED=True,
            SESSION_COOKIE_SECURE=False, OBSIDIAN_VAULT_PATH='', ACCOUNT_ACTIVITY_ENABLED=False), patch.object(server, 'engine', self.engine),
            patch.object(server, 'accounts', AccountStore(self.engine)),
            patch('paper_server.daily_review.today', return_value=date(2026, 10, 28))]
        for item in self.patches:
            item.start()
        self.admin, self.member, self.viewer = self.client(1), self.client(2), self.client(3)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.engine.dispose()

    def client(self, account):
        client = server.app.test_client()
        with client.session_transaction() as session:
            session.update(authenticated=True, account_id=account, session_version=1,
                csrf_token='review-csrf')
        return client

    def daily(self, client=None):
        response = (client or self.admin).get('/api/daily-review')
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def save(self, item, client=None, **values):
        body = dict(article_number=item['article_number'], notes='핵심 아이디어\n장점과 한계',
                    status='reading', version=item['version'])
        body.update(values)
        return (client or self.admin).put('/api/daily-review/review', json=body,
            headers={'X-CSRF-Token':'review-csrf'})

    def test_picks_are_distinct_favorites_and_persist_across_store_instances(self):
        first = self.daily()
        self.assertEqual(len(first['items']), 2)
        ids = [p['article_number'] for p in first['items']]
        self.assertEqual(len(set(ids)), 2)
        self.assertTrue(set(ids) <= {'1', '2', '3'})
        self.assertEqual(self.daily()['items'], first['items'])
        second = DailyReviewStore(self.engine).daily('account:1', date(2026, 10, 28))
        self.assertEqual([p['article_number'] for p in second['items']], ids)
        self.assertEqual(first['timezone'], 'Asia/Seoul')
        self.assertEqual(first['remaining'], 3)

    def test_unfinished_recommendations_remain_eligible_and_completion_is_reversible(self):
        samples = []
        def sample(candidates, k):
            samples.append(set(candidates))
            return sorted(candidates)[:k]
        with patch('paper_server.daily_review.random.SystemRandom.sample', side_effect=sample):
            first = self.daily()
            item = first['items'][0]
            saved = self.save(item, status='completed')
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(self.daily()['items'][0]['status'], 'completed')
            self.assertEqual(self.daily()['remaining'], 2)
            with patch('paper_server.daily_review.today', return_value=date(2026, 10, 29)):
                next_day = self.daily()
            self.assertNotIn(item['article_number'], samples[1])
            self.assertIn(first['items'][1]['article_number'], samples[1])
            self.assertEqual(len(next_day['items']), 2)
            reopened = self.save(saved.json['item'], status='reading')
            self.assertEqual(reopened.status_code, 200)
            with patch('paper_server.daily_review.today', return_value=date(2026, 10, 30)):
                self.daily()
            self.assertIn(item['article_number'], samples[2])

    def test_draft_notes_survive_refresh_and_repeat_assignment(self):
        with patch('paper_server.daily_review.random.SystemRandom.sample', side_effect=lambda c,k: sorted(c)[:k]):
            item = self.daily()['items'][0]
            response = self.save(item, notes='<script>alert(1)</script>\n개인 메모')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['item']['version'], 1)
            self.assertTrue(response.json['item']['updated_at'].endswith('+00:00'))
            with patch('paper_server.daily_review.today', return_value=date(2026, 10, 29)):
                again = self.daily()['items'][0]
            self.assertEqual(again['notes'], '<script>alert(1)</script>\n개인 메모')
            history = self.admin.get('/api/daily-review/history').json
            self.assertEqual(history['total'], 2)
            self.assertEqual(history['items'][0]['last_recommended'], '2026-10-29')
            self.assertEqual(history['items'][0]['notes'], again['notes'])
            self.assertEqual(again['status'], 'reading')

    def test_accounts_ignore_forged_identity_and_linked_viewer_keeps_own_notes(self):
        admin = self.daily()
        member = self.member.get('/api/daily-review?account_id=1&owner=account:1').json
        self.assertEqual({i['article_number'] for i in member['items']}, {'4', '5'})
        self.assertEqual(self.save(admin['items'][0], client=self.member).status_code, 403)
        self.assertEqual(self.save(admin['items'][0], notes='관리자 개인 리뷰').status_code, 200)
        viewer = self.daily(self.viewer)
        self.assertTrue({i['article_number'] for i in viewer['items']} <= {'1','2','3'})
        self.assertTrue(all(not i['notes'] for i in viewer['items']))
        self.assertEqual(self.save(viewer['items'][0], client=self.viewer,
            notes='독자의 개인 리뷰', owner='account:1', account_id=1).status_code, 403)
        admin_notes = [i['notes'] for i in self.daily()['items']]
        self.assertIn('관리자 개인 리뷰', admin_notes)
        self.assertNotIn('독자의 개인 리뷰', admin_notes)
        self.assertEqual(self.member.get('/api/daily-review/history').json['total'], 2)

    def test_empty_and_single_candidate_days(self):
        with self.engine.begin() as conn:
            conn.execute(text('DELETE FROM account_favorites WHERE account_id=1'))
        self.assertEqual(self.daily()['items'], [])
        with self.engine.begin() as conn:
            conn.execute(text('INSERT INTO account_favorites VALUES (1,1)'))
        self.assertEqual(self.daily()['items'], [])  # A day's selection stays fixed.
        with patch('paper_server.daily_review.today', return_value=date(2026, 10, 29)):
            only = self.daily()
        self.assertEqual(len(only['items']), 1)
        self.assertEqual(only['items'][0]['article_number'], '1')

    def test_optimistic_version_preserves_notes_when_two_tabs_save(self):
        item = self.daily()['items'][0]
        saved = self.save(item, notes='첫 번째 창')
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(self.save(item, notes='오래된 다른 창').status_code, 409)
        self.assertEqual(self.save(saved.json['item'], notes='두 번째 수정').status_code, 200)
        self.assertEqual(self.save(saved.json['item'], notes='이미 낡은 수정').status_code, 409)
        current = next(i for i in self.daily()['items'] if i['article_number'] == item['article_number'])
        self.assertEqual(current['notes'], '두 번째 수정')

    def test_authentication_csrf_input_validation_and_private_cache_headers(self):
        anonymous = server.app.test_client()
        self.assertEqual(anonymous.get('/daily-review').status_code, 302)
        for path in ('/api/daily-review', '/api/daily-review/history'):
            self.assertEqual(anonymous.get(path).status_code, 401)
        self.assertEqual(anonymous.put('/api/daily-review/review', json={}).status_code, 401)
        self.assertEqual(self.admin.put('/api/daily-review/review', json={}).status_code, 403)
        item = self.daily()['items'][0]
        for invalid in ({'notes':123}, {'notes':'x' * 12001}, {'status':[]}, {'version':True},
                        {'version':-1}, {'article_number':[]}, {'status':'dismissed'}):
            self.assertEqual(self.save(item, **invalid).status_code, 400, invalid)
        for page in ('0', '-1', 'a', '100001', '９', '9' * 5000):
            self.assertEqual(self.admin.get('/api/daily-review/history?page=' + page).status_code, 400)
        response = self.admin.get('/daily-review')
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertIn(b'daily_review.js', response.data)
        self.assertIn(b'href="/daily-review"', self.admin.get('/').data)

    def test_seoul_date_rolls_over_at_utc_1500(self):
        # The module's real clock applies the named zone before taking the date.
        import paper_server.daily_review as module
        for utc_hour, expected in ((14, date(2026,10,28)), (15, date(2026,10,29))):
            instant = datetime(2026,10,28,utc_hour,0,tzinfo=timezone.utc)
            with patch.object(module, 'datetime') as clock:
                clock.now.side_effect = lambda zone: instant.astimezone(zone)
                # Stop only our setUp date patch to exercise the actual date function.
                self.patches[-1].stop()
                try:
                    self.assertEqual(module.today(), expected)
                finally:
                    self.patches[-1].start()

    def test_legacy_configured_logins_have_distinct_private_notebooks(self):
        with patch.dict(server.app.config, ACCOUNT_AUTH_ENABLED=False):
            clients = []
            for username in ('admin', 'owner-b'):
                client = server.app.test_client()
                with client.session_transaction() as session:
                    session.update(authenticated=True, username=username, role='admin', csrf_token='review-csrf')
                clients.append(client)
            one, two = self.daily(clients[0]), self.daily(clients[1])
            self.assertEqual(self.save(one['items'][0], clients[0], notes='legacy private').status_code, 200)
            self.assertTrue(all(not item['notes'] for item in self.daily(clients[1])['items']))
            self.assertEqual(self.save(two['items'][0], clients[1]).status_code, 403)
            self.assertEqual(len(two['items']), 2)

    def test_only_named_root_can_write_even_if_another_account_is_admin(self):
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE accounts SET role='admin' WHERE id=2"))
        item = self.daily(self.member)['items'][0]
        with self.member.session_transaction() as session:
            session['username'] = 'admin'
        self.assertEqual(self.save(item, client=self.member).status_code, 403)
        with self.assertRaises(PermissionError):
            DailyReviewStore(self.engine).save('account:2', item['article_number'], 'forged', 'reading', 0)
        page = self.member.get('/daily-review')
        self.assertIn(b'data-can-write-reviews="false"', page.data)
        self.assertIn('admin'.encode(), page.data)
        self.assertFalse(self.daily(self.member)['can_write_reviews'])
        self.assertTrue(self.daily()['can_write_reviews'])

    def test_obsidian_retry_is_root_only_and_keeps_existing_review_version(self):
        item = self.daily()['items'][0]
        saved = self.save(item)
        self.assertEqual(saved.status_code, 200)
        payload = {'article_number':item['article_number']}
        for client, expected in ((self.member,403),(self.viewer,403),(self.admin,503)):
            response = client.post('/api/daily-review/obsidian-sync',json=payload,
                headers={'X-CSRF-Token':'review-csrf'})
            self.assertEqual(response.status_code,expected)
        self.assertEqual(self.admin.post('/api/daily-review/obsidian-sync',json=payload).status_code,403)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text('SELECT version FROM personal_paper_reviews')).scalar(),1)

    def test_status_only_save_preserves_legacy_notes_and_optimistic_version(self):
        item=self.daily()['items'][0]
        saved=self.save(item,notes='기존 웹 메모 보존').json['item']
        payload=dict(article_number=item['article_number'],status='completed',version=saved['version'])
        response=self.admin.put('/api/daily-review/review',json=payload,headers={'X-CSRF-Token':'review-csrf'})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['item']['notes'],'기존 웹 메모 보존')
        self.assertEqual(response.json['item']['status'],'completed')
        self.assertEqual(self.admin.put('/api/daily-review/review',json=payload,headers={'X-CSRF-Token':'review-csrf'}).status_code,409)

    def test_concurrent_first_visits_share_one_complete_batch(self):
        # Separate connections contend on the persisted date key, as multiple workers do.
        workspace_tmp = Path(__file__).resolve().parents[1] / 'tmp'
        workspace_tmp.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='daily-review-test-', dir=workspace_tmp) as folder:
            self.assertTrue(Path(folder).resolve().is_relative_to(workspace_tmp.resolve()))
            engine = create_engine('sqlite:///' + folder.replace('\\', '/') + '/review.db')
            try:
                with engine.begin() as conn:
                    conn.execute(text('CREATE TABLE papers (article_number TEXT PRIMARY KEY,title TEXT,'
                        'authors TEXT,year TEXT,source_name TEXT,source_system TEXT,url TEXT,'
                        'pdf_available INT,is_favorite INT)'))
                    for i in range(3):
                        conn.execute(text("INSERT INTO papers VALUES (:id,'Paper','Author','2026',"
                            "'JSSC','ieee','https://example.test',1,1)"), {'id':str(i)})
                metadata.create_all(engine)
                barrier = threading.Barrier(2)
                def sample(candidates, count):
                    barrier.wait(timeout=5)
                    return candidates[:count]
                with patch('paper_server.daily_review.random.SystemRandom.sample', side_effect=sample):
                    with ThreadPoolExecutor(max_workers=2) as workers:
                        futures = [workers.submit(DailyReviewStore(engine).daily,
                            'legacy:concurrent', date(2026,10,28)) for _ in range(2)]
                        result = [future.result(timeout=10) for future in futures]
                self.assertEqual(result[0]['items'], result[1]['items'])
                self.assertEqual(len(result[0]['items']), 2)
                with engine.connect() as conn:
                    self.assertEqual(conn.execute(text('SELECT COUNT(*) FROM daily_review_batches')).scalar(), 1)
                    self.assertEqual(conn.execute(text('SELECT COUNT(*) FROM daily_review_picks')).scalar(), 2)
            finally:
                engine.dispose()

    def test_storage_failure_is_reported_without_exposing_sql(self):
        with self.engine.begin() as conn:
            conn.execute(text('DROP TABLE personal_paper_reviews'))
        response = self.admin.get('/api/daily-review')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('SELECT', response.json['error'])


if __name__ == '__main__':
    unittest.main()
