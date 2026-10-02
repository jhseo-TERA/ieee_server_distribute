from copy import deepcopy
from datetime import datetime
import threading
import unittest
from unittest.mock import Mock, patch

from local_ai_recommendations import (
    RecommendationService, VERSION, balanced, select_mode, validate_profile, validated_ranks,
)
import web.app as app_module


class RecommendationRulesTests(unittest.TestCase):
    def setUp(self):
        self.seeds = [{'article_number': 'fav1', 'title': 'PAM4 receiver', 'source_name': 'JSSC'}]
        self.profile = {'summary': '수신기', 'topics': [{'name': '수신기', 'keywords': ['receiver'], 'favorite_ids': ['fav1']}]}
        self.candidates = [{'article_number': 'p1', 'title': 'PAM4 receiver with DFE', 'abstract': ''}]
        self.rank = {'id': 'p1', 'score': 87, 'novelty': 42, 'topic': '수신기',
                     'favorite_id': 'fav1', 'reason': '수신기 구조가 유사합니다.', 'evidence': 'receiver with DFE'}

    def test_balanced_sampling_includes_small_venues(self):
        rows = [{'source_name': 'large', 'id': i} for i in range(100)] + [{'source_name': 'small', 'id': 999}]
        sample = balanced(rows, 4)
        self.assertIn(999, [row['id'] for row in sample])
        self.assertEqual(len(sample), 4)

    def test_profile_needs_real_favorite_evidence(self):
        self.assertEqual(validate_profile(self.profile, self.seeds), self.profile)
        invalid = deepcopy(self.profile)
        invalid['topics'][0]['favorite_ids'] = ['invented']
        with self.assertRaises(ValueError):
            validate_profile(invalid, self.seeds)

    def test_rank_validation_never_accepts_model_generated_identity(self):
        valid, bad = validated_ranks({'rankings': [self.rank]}, self.candidates, self.profile, self.seeds)
        self.assertEqual(valid['p1']['ai_score'], 87)
        self.assertEqual(valid['p1']['ai_evidence_basis'], 'title_only')
        self.assertEqual(bad, 0)
        for change in ({'id': 'invented'}, {'favorite_id': 'invented'}, {'topic': 'invented'},
                       {'score': True}, {'score': float('nan')}, {'score': 101}, {'novelty': -1},
                       {'evidence': 'world best 0.1 pJ/bit'}, {'evidence': '----'}):
            with self.subTest(change=change):
                valid, bad = validated_ranks({'rankings': [{**self.rank, **change}]}, self.candidates, self.profile, self.seeds)
                self.assertEqual(valid, {})
                self.assertEqual(bad, 1)

    def test_duplicate_rank_ids_are_rejected_not_double_counted(self):
        valid, bad = validated_ranks({'rankings': [self.rank, self.rank]}, self.candidates, self.profile, self.seeds)
        self.assertEqual(valid, {})
        self.assertEqual(bad, 2)

    def test_reference_must_belong_to_topic(self):
        seeds = self.seeds + [{'article_number': 'fav2', 'title': 'unrelated', 'source_name': 'Other'}]
        valid, _ = validated_ranks({'rankings': [{**self.rank, 'favorite_id': 'fav2'}]}, self.candidates, self.profile, seeds)
        self.assertEqual(valid, {})

    def test_modes_reorder_and_keep_limits_without_mutating_cache(self):
        rows = [
            {'article_number': 'old', 'source_name': 'JSSC', 'year': '2020', 'ai_score': 90,
             'ai_novelty': 10, 'ai_topic': 'receiver', 'survey_included': True, 'survey_missing': []},
            {'article_number': 'new', 'source_name': 'JSSC', 'year': '2026', 'ai_score': 75,
             'ai_novelty': 90, 'ai_topic': 'optical', 'survey_included': True, 'survey_missing': ['공정']},
            {'article_number': 'excluded', 'source_name': 'JSSC', 'year': '2026', 'ai_score': 50,
             'ai_novelty': 10, 'survey_included': False, 'survey_missing': ['에너지']},
        ]
        original = deepcopy(rows)
        self.assertEqual(select_mode(rows, 'match', 1, 2026)[0]['article_number'], 'old')
        self.assertEqual(select_mode(rows, 'recent', 1, 2026)[0]['article_number'], 'new')
        self.assertEqual(select_mode(rows, 'explore', 1, 2026)[0]['article_number'], 'new')
        self.assertEqual([r['article_number'] for r in select_mode(rows, 'survey', 15)], ['new'])
        self.assertEqual(rows, original)

    def test_diversity_avoids_repeating_topic_when_good_alternative_exists(self):
        rows = [{'article_number': str(i), 'source_name': 'JSSC', 'ai_score': score, 'ai_topic': topic}
                for i, score, topic in [(1, 95, 'RX'), (2, 94, 'RX'), (3, 88, 'TX')]]
        result = select_mode(rows, 'diverse', 2)
        self.assertEqual([r['ai_topic'] for r in result], ['RX', 'TX'])


class RecommendationCacheTests(unittest.TestCase):
    def setUp(self):
        self.worker = Mock()
        self.service = RecommendationService(Mock(), self.worker, Mock())
        self.addCleanup(self.service.close)
        self.snap = {'signature': 'sig', 'favorites': [{'article_number': 'f', 'title': 'favorite'}]}
        self.service.snapshot = Mock(return_value=self.snap)
        self.service.schedule_refresh = Mock(return_value=True)

    def job(self, status='complete'):
        return {'id': 'job', 'status': status, 'updated_at': datetime.now().isoformat(),
                'request': {'signature': 'sig'}, 'result': {}, 'error': None}

    def test_refresh_reuses_active_job_even_when_forced(self):
        self.service.latest = Mock(return_value=self.job('running'))
        self.assertEqual(self.service.refresh(force=True)['status'], 'running')
        self.worker.enqueue.assert_not_called()

    def test_refresh_reuses_job_owned_by_another_worker_process(self):
        self.worker._start.side_effect = RuntimeError('worker lock is held')
        self.service.latest = Mock(return_value=self.job('running'))
        self.assertEqual(self.service.refresh(force=True)['status'], 'running')
        self.worker.enqueue.assert_not_called()

    def test_worker_lock_failure_without_active_job_is_not_hidden(self):
        self.worker._start.side_effect = RuntimeError('worker lock is held')
        self.service.latest = Mock(return_value=self.job('complete'))
        with self.assertRaises(RuntimeError):
            self.service.refresh(force=True)
        self.worker.enqueue.assert_not_called()

    def test_fresh_cache_never_triggers_inference(self):
        self.service.latest = Mock(return_value=self.job())
        self.assertEqual(self.service.refresh()['status'], 'fresh')
        self.worker.enqueue.assert_not_called()

    def test_failure_cooldown_avoids_repeated_model_calls(self):
        self.service.latest = Mock(return_value=self.job('failed'))
        self.assertEqual(self.service.refresh()['status'], 'cooldown')
        self.worker.enqueue.assert_not_called()

    def test_disabled_recommendations_never_start_worker(self):
        with patch.dict('os.environ', {'LOCAL_AI_RECO_ENABLED': '0'}):
            self.assertEqual(self.service.refresh()['status'], 'disabled')
        self.worker._start.assert_not_called()

    def test_sql_candidates_are_returned_while_ai_is_queued(self):
        job = self.job('queued')
        self.service.latest = Mock(side_effect=lambda signature=None, complete=False: None if complete else job)
        self.service.refresh = Mock(return_value={'status': 'queued'})
        self.service.sql_builder.return_value = {'items': [{'article_number': 'p', 'id': 1, 'source_name': 'JSSC',
                                                            'title': 'PAM4 receiver', 'favorite_similarity': .8}],
                                                 'groups': [{'name': 'JSSC'}]}
        self.service.enrich = Mock(side_effect=lambda rows: deepcopy(rows))
        self.service.sql_cache.debounce = 0
        self.service.view()  # Cold view schedules SQL but does not wait for it.
        with self.service.sql_cache.condition:
            self.assertTrue(self.service.sql_cache.condition.wait_for(
                lambda: bool(self.service.sql_cache.entries), timeout=2))
        view = self.service.view()
        self.assertEqual(view['engine'], 'sql')
        self.assertEqual(view['items'][0]['recommendation_basis'], 'deterministic')
        self.assertEqual(view['ai']['status'], 'queued')
        self.worker._chat.assert_not_called()

    def test_stale_ai_is_served_without_waiting_and_new_favorites_are_removed(self):
        old = self.job()
        old['request']['signature'] = 'old'
        old['result'] = {'version': VERSION, 'items': [
            {'article_number': 'f', 'title': 'favorite', 'source_name': 'JSSC'},
            {'article_number': 'p', 'title': 'New receiver', 'source_name': 'JSSC', 'ai_score': 90},
        ], 'groups': [{'name': 'JSSC'}]}
        self.service.latest = Mock(side_effect=lambda signature=None, complete=False: None if signature else old)
        self.service.enrich = Mock(side_effect=deepcopy)
        self.service.sql_cache.debounce = 60
        view = self.service.view()
        self.assertEqual([p['article_number'] for p in view['items']], ['p'])
        self.assertTrue(view['ai']['stale'])
        self.assertEqual(view['refresh']['status'], 'ready')
        self.assertEqual(view['engine'], 'hybrid')
        self.service.sql_builder.assert_not_called()
        self.assertEqual(len(old['result']['items']), 2)

    def test_stale_ai_never_oscillates_with_sql_cache_and_swaps_only_when_complete(self):
        old = self.job()
        old['request']['signature'] = 'old'
        old['result'] = {'version': VERSION, 'items': [
            {'article_number': 'a', 'title': 'Optical receiver', 'source_name': 'OFC', 'ai_score': 90},
        ], 'groups': [{'name': 'OFC'}]}
        self.service.latest = Mock(side_effect=lambda signature=None, complete=False: None if signature else old)
        self.service.enrich = Mock(side_effect=deepcopy)
        self.service.sql_cache.get = Mock()
        for status in ('refreshing', 'ready', 'refreshing', 'failed', 'ready'):
            self.service.sql_cache.get.return_value = ({'items': [{'article_number': 'sql', 'source_name': 'JSSC'}],
                                                       'groups': [{'name': 'JSSC'}]}, {'status': status})
            view = self.service.view()
            self.assertEqual(view['engine'], 'hybrid')
            self.assertEqual([p['article_number'] for p in view['items']], ['a'])
            self.assertTrue(view['ai']['stale'])
        self.service.sql_cache.get.assert_not_called()
        fresh = deepcopy(old)
        fresh['request']['signature'] = 'sig'
        fresh['result']['items'][0]['article_number'] = 'new'
        self.service.latest = Mock(return_value=fresh)
        view = self.service.view()
        self.assertEqual([p['article_number'] for p in view['items']], ['new'])
        self.assertFalse(view['ai']['stale'])

    def test_empty_stale_ai_is_not_refilled_with_sql(self):
        old = self.job()
        old['result'] = {'version': VERSION, 'items': [], 'groups': []}
        self.service.latest = Mock(side_effect=lambda signature=None, complete=False: None if signature else old)
        self.service.enrich = Mock(side_effect=deepcopy)
        self.service.sql_cache.get = Mock()
        view = self.service.view()
        self.assertEqual(view['engine'], 'hybrid')
        self.assertEqual(view['items'], [])
        self.service.sql_cache.get.assert_not_called()

    def test_fresh_ai_does_not_schedule_sql_or_ai(self):
        job = self.job()
        job['result'] = {'version': VERSION, 'items': [], 'groups': []}
        self.service.latest = Mock(return_value=job)
        self.service.enrich = Mock(return_value=[])
        self.service.view()
        self.service.schedule_refresh.assert_not_called()
        self.service.sql_builder.assert_not_called()

    def test_compatible_previous_version_stays_visible_but_never_fresh(self):
        job = self.job()
        job['result'] = {'version': 'favorites-ai-3', 'items': [
            {'article_number': 'p', 'title': 'Optical receiver', 'source_name': 'OFC', 'ai_score': 90}
        ], 'groups': [{'name': 'OFC'}]}
        self.service.latest = Mock(return_value=job)
        self.service.enrich = Mock(side_effect=deepcopy)
        self.service.sql_cache.get = Mock()
        with patch('local_ai_recommendations.VERSION', 'favorites-ai-4'):
            view = self.service.view()
        self.assertEqual(view['engine'], 'hybrid')
        self.assertEqual(view['items'][0]['article_number'], 'p')
        self.assertTrue(view['ai']['stale'])
        self.assertEqual(view['ai']['version'], 'favorites-ai-4')
        self.assertEqual(view['ai']['result_version'], 'favorites-ai-3')
        self.service.schedule_refresh.assert_called_once()
        self.service.sql_cache.get.assert_not_called()

    def test_empty_favorites_never_show_old_recommendations(self):
        self.snap['favorites'] = []
        job = self.job()
        job['result'] = {'items': [{'article_number': 'p'}]}
        self.service.latest = Mock(return_value=job)
        self.assertEqual(self.service.view()['items'], [])
        self.service.schedule_refresh.assert_not_called()


class RecommendationRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = app_module.app
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        self.bp = self.app.blueprints['local_ai']
        self.service = Mock()
        self.factory = patch.object(self.bp, 'ai_service_factory', return_value=self.service)
        self.factory_mock = self.factory.start()
        self.sql_view = patch.object(app_module.sql_recommendations, 'view',
                                     return_value={'items': [], 'groups': [], 'engine': 'sql', 'ai': {}})
        self.sql_view_mock = self.sql_view.start()

    def tearDown(self):
        self.sql_view.stop()
        self.factory.stop()

    def login(self, role):
        with self.client.session_transaction() as session:
            session.update(authenticated=True, username='test-user', role=role, csrf_token='test-csrf')

    def test_unauthenticated_recommendations_require_login(self):
        self.assertEqual(self.client.get('/api/recommendations').status_code, 401)

    def test_viewer_can_read_all_modes_but_cannot_force_regeneration(self):
        self.login('viewer')
        response = self.client.get('/api/recommendations?mode=explore')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
        self.sql_view_mock.assert_called_once_with('explore', 20)
        self.factory_mock.assert_not_called()
        response = self.client.post('/api/recommendations/refresh', headers={'X-CSRF-Token': 'test-csrf'})
        self.assertEqual(response.status_code, 403)
        self.factory_mock.assert_not_called()

    def test_admin_regeneration_requires_csrf(self):
        self.login('admin')
        self.assertEqual(self.client.post('/api/recommendations/refresh').status_code, 403)
        response = self.client.post('/api/recommendations/refresh', headers={'X-CSRF-Token': 'test-csrf'})
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json['status'], 'disabled')
        self.factory_mock.assert_not_called()

    def test_invalid_mode_is_rejected(self):
        self.login('viewer')
        self.assertEqual(self.client.get('/api/recommendations?mode=arbitrary').status_code, 400)

    def test_sql_failure_is_safe_and_does_not_start_ai(self):
        self.login('viewer')
        self.sql_view_mock.side_effect = RuntimeError('private error')
        response = self.client.get('/api/recommendations')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private error', response.get_data(as_text=True))
        self.factory_mock.assert_not_called()
