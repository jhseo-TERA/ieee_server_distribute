import threading
import time
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, event, text

from recommendation_cache import RecommendationCache
from local_ai_recommendations import RecommendationService
import web.app as app_module


class BackgroundCacheTests(unittest.TestCase):
    def cache(self, builder, **kwargs):
        cache = RecommendationCache(builder, **kwargs)
        self.addCleanup(cache.close)
        return cache

    def ready(self, cache, key):
        with cache.condition:
            self.assertTrue(cache.condition.wait_for(
                lambda: cache.entries.get(15, (None,))[0] == key, timeout=2))

    def test_slow_sql_does_not_block_cold_or_concurrent_reads(self):
        started, release = threading.Event(), threading.Event()
        def build(n):
            started.set()
            release.wait(3)
            return {'items': [n]}
        cache = self.cache(Mock(side_effect=build), debounce=0)
        self.addCleanup(release.set)
        self.assertEqual(cache.get('a', 15)[1]['status'], 'refreshing')
        self.assertTrue(started.wait(1))
        start = time.monotonic()
        for _ in range(20):
            self.assertIsNone(cache.get('a', 15)[0])
        self.assertLess(time.monotonic() - start, .5)
        self.assertEqual(cache.builder.call_count, 1)
        release.set()
        self.ready(cache, 'a')
        self.assertEqual(cache.get('a', 15)[0], {'items': [15]})

    def test_favorite_burst_is_coalesced_and_old_result_stays_available(self):
        cache = self.cache(Mock(return_value={'items': ['old']}), debounce=0)
        cache.get('a', 15)
        self.ready(cache, 'a')
        cache.debounce = .05
        for i in range(20):
            result, status = cache.get(str(i), 15)
            self.assertEqual(result['items'], ['old'])
            self.assertTrue(status['stale'])
        self.ready(cache, '19')
        self.assertEqual(cache.builder.call_count, 2)

    def test_superseded_running_result_is_not_published(self):
        started, release = threading.Event(), threading.Event()
        def build(n):
            started.set()
            release.wait(3)
            return {'items': [n]}
        cache = self.cache(Mock(side_effect=build), debounce=0)
        self.addCleanup(release.set)
        cache.get('a', 15)
        self.assertTrue(started.wait(1))
        cache.get('b', 15)
        release.set()
        self.ready(cache, 'b')
        self.assertEqual(cache.builder.call_count, 2)

    def test_failure_has_backoff_and_keeps_previous_results(self):
        cache = self.cache(Mock(return_value={'items': ['old']}), debounce=0)
        cache.get('a', 15)
        self.ready(cache, 'a')
        cache.builder.side_effect = RuntimeError('private SQL error')
        with self.assertLogs('recommendation_cache', level='ERROR'):
            cache.get('b', 15)
            with cache.condition:
                self.assertTrue(cache.condition.wait_for(lambda: bool(cache.errors), timeout=2))
        for _ in range(10):
            result, status = cache.get('b', 15)
            self.assertEqual(result['items'], ['old'])
            self.assertEqual(status['status'], 'failed')
            self.assertNotIn('private', status['message'])
        self.assertEqual(cache.builder.call_count, 2)

    def test_ai_debounce_uses_latest_snapshot_once(self):
        service = RecommendationService(Mock(), Mock(), Mock())
        self.addCleanup(service.close)
        service.refresh = Mock()
        timers = []
        def timer(delay, callback):
            obj = Mock(callback=callback)
            timers.append(obj)
            return obj
        with patch('local_ai_recommendations.threading.Timer', side_effect=timer):
            service.schedule_refresh({'signature': 'a'})
            service.schedule_refresh({'signature': 'a'})
            service.schedule_refresh({'signature': 'b'})
        self.assertEqual(len(timers), 2)
        timers[0].cancel.assert_called_once()
        timers[0].callback()
        service.refresh.assert_not_called()
        timers[1].callback()
        service.refresh.assert_called_once_with()


class FavoriteBulkTests(unittest.TestCase):
    def setUp(self):
        # SQLite checks actual transaction results. MySQL row locks are preserved
        # in production; only this test adapter strips its unsupported FOR UPDATE.
        self.engine = create_engine('sqlite://')
        self.addCleanup(self.engine.dispose)
        self.statements = []
        @event.listens_for(self.engine, 'before_cursor_execute', retval=True)
        def adapt(conn, cursor, statement, params, context, many):
            self.statements.append(statement)
            return statement.replace(' FOR UPDATE', ''), params
        with self.engine.begin() as conn:
            conn.execute(text('CREATE TABLE papers(article_number TEXT PRIMARY KEY, is_favorite INTEGER, '
                              'citation_count INTEGER, citation_source TEXT, citation_updated_at TEXT)'))
            conn.execute(text('INSERT INTO papers VALUES(:a,0,7,"test","2026-09-07")'),
                         [{'a': str(i)} for i in range(240)])
        self.patch = patch.object(app_module, 'engine', self.engine)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.client = app_module.app.test_client()
        app_module.app.config.update(TESTING=True)
        self.login('admin')
        self.headers = {'X-CSRF-Token': 'test-token'}
        self.statements.clear()

    def login(self, role):
        with self.client.session_transaction() as sess:
            sess.update(authenticated=True, username='test', role=role, csrf_token='test-token')

    def post(self, ids):
        return self.client.post('/api/favorites/bulk', json={'article_numbers': ids}, headers=self.headers)

    def test_bulk_240_uses_one_update_preserves_citations_and_is_idempotent(self):
        response = self.post([str(i) for i in range(240)])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['changed_count'], 240)
        self.assertEqual(len(self.statements), 2)  # One locked SELECT, one UPDATE.
        response = self.post(['1', '1', ' 2 '])
        self.assertEqual(response.json['changed_count'], 0)
        self.assertEqual(response.json['article_numbers'], ['1', '2'])
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text('SELECT SUM(is_favorite) FROM papers')).scalar(), 240)
            self.assertEqual(conn.execute(text('SELECT COUNT(*) FROM papers WHERE citation_count=7')).scalar(), 240)

    def test_unknown_paper_rejects_entire_batch_without_changes(self):
        self.assertEqual(self.post(['1', 'missing']).status_code, 404)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text('SELECT SUM(is_favorite) FROM papers')).scalar(), 0)

    def test_invalid_input_permissions_and_csrf_never_write(self):
        for ids in ([], '1', [None], [1], [''], ['x' * 256], ['1'] * 1001):
            self.assertEqual(self.post(ids).status_code, 400)
        self.assertEqual(self.client.post('/api/favorites/bulk', json={'article_numbers': ['1']}).status_code, 403)
        self.login('viewer')
        self.assertEqual(self.post(['1']).status_code, 403)
        self.assertEqual(self.statements, [])

    def test_failed_update_rolls_back(self):
        @event.listens_for(self.engine, 'after_cursor_execute')
        def fail(conn, cursor, statement, params, context, many):
            if statement.startswith('UPDATE'):
                raise RuntimeError('simulated failure after update')
        with self.assertRaises(RuntimeError):
            self.post(['1', '2'])
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text('SELECT SUM(is_favorite) FROM papers')).scalar(), 0)

    def test_single_explicit_setting_is_retry_safe_and_legacy_toggle_still_works(self):
        for expected in (1, 0):
            result = self.client.post('/api/favorite', json={'article_number': '1', 'favorite': True}, headers=self.headers)
            self.assertEqual(result.json['favorite_delta'], expected)
        result = self.client.post('/api/favorite', json={'article_number': '1'}, headers=self.headers)
        self.assertEqual(result.json['favorite_delta'], -1)
        self.assertIsNone(result.json['citation_count'])


if __name__ == '__main__':
    unittest.main()
