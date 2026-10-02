from copy import deepcopy
from collections import Counter
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, event, text
from recommendation_feedback import FeedbackStore, metadata
from recommendation_policy import (FavoriteSimilarity, allocate, weighted_profile, near_duplicate,
                                   excluded_electrical_multicarrier)
from local_ai_recommendations import RecommendationService, VERSION, select_mode, select_sql_mode, validated_ranks
import web.app as app


class QualityTests(unittest.TestCase):
    def test_sql_modes_choose_different_papers_from_one_pool(self):
        rows = [
            {'article_number':'old-fit','source_name':'JSSC','title':'PAM4 receiver','year':'2010','favorite_similarity':.95},
            {'article_number':'new','source_name':'JSSC','title':'PAM4 receiver','year':'2026','favorite_similarity':.65},
            {'article_number':'rare','source_name':'JSSC','title':'Optical microring modulator','year':'2020','favorite_similarity':.70},
            {'article_number':'generic','source_name':'JSSC','title':'The Cross-Coupled Pair','year':'2026','favorite_similarity':1.0},
        ]
        topics = {'수신기':100, '광 I/O':1}
        self.assertEqual(select_sql_mode(rows,'match',1,topics,2026)[0]['article_number'],'old-fit')
        self.assertEqual(select_sql_mode(rows,'recent',1,topics,2026)[0]['article_number'],'new')
        self.assertEqual(select_sql_mode(rows,'explore',1,topics,2026)[0]['article_number'],'rare')
        diverse = select_sql_mode(rows,'diverse',2,topics,2026)
        self.assertEqual({r['article_number'] for r in diverse},{'old-fit','rare'})
        self.assertNotIn('generic',{r['article_number'] for r in select_sql_mode(rows,'match',20,topics,2026)})

    def test_electrical_multicarrier_exclusion_keeps_optical_and_single_carrier_io(self):
        rejected = [
            '7.5 A 353mW 112Gb/s Discrete Multitone Wireline Receiver Datapath with Time-Based ADC in 5nm FinFET',
            'A DMT-Based Electrical I/O Transceiver',
            'OFDM for Copper Backplane Links',
            'A Multi-Carrier Die-to-Die Transmitter',
            'A Discrete Multi-Tone Wireline Transmitter',
        ]
        kept = [
            'A PAM-4 Electrical I/O Transceiver with Adaptive Equalizer',
            'A 52-Gb/s Capacitively Driven PAM-4 Transceiver for Die-to-Die Interfaces',
            'A CDR for NRZ SerDes',
            'HBM Interfaces with Crosstalk Cancellation',
            'A 400 Gb/s O-band WDM Silicon Photonic Ring Modulator-based Transceiver',
            'An Optical OFDM Transceiver',
            'DMT for Short-Reach Optical Interconnects',
            'A Multi-Channel PAM4 Electrical Receiver',
            'A DMT-Free Wireline Transceiver',
            'PAM4 versus DMT for Electrical Links',
        ]
        for title in rejected:
            with self.subTest(title=title):
                self.assertTrue(excluded_electrical_multicarrier({'title':title}))
        for title in kept:
            with self.subTest(title=title):
                self.assertFalse(excluded_electrical_multicarrier({'title':title}))

    def test_multicarrier_abstract_mentions_do_not_exclude_pam_work(self):
        self.assertFalse(excluded_electrical_multicarrier({
            'title':'A PAM4 Wireline Receiver', 'abstract':'Prior work uses DMT for electrical links.'}))
        self.assertTrue(excluded_electrical_multicarrier({
            'title':'A Discrete Multitone Transmitter', 'abstract':'This transmitter targets electrical die-to-die links.'}))
        self.assertFalse(excluded_electrical_multicarrier({
            'title':'A Discrete Multitone Transmitter', 'abstract':'This transmitter targets optical interconnects.'}))

    def test_excluded_topics_cannot_train_profile_or_leak_from_cached_high_scores(self):
        rejected = {'article_number':'dmt', 'source_name':'JSSC', 'title':'Discrete Multitone Wireline Receiver', 'ai_score':100}
        allowed = {'article_number':'pam', 'source_name':'JSSC', 'title':'PAM4 Electrical Receiver', 'ai_score':80}
        self.assertNotIn('multitone', weighted_profile([rejected,allowed], app._title_terms))
        self.assertEqual([r['article_number'] for r in FavoriteSimilarity([rejected,allowed], app._title_terms).rows], ['pam'])
        self.assertEqual([r['article_number'] for r in allocate([rejected,allowed])], ['pam'])
        for mode in ['match','explore','recent','diverse']:
            self.assertEqual([r['article_number'] for r in select_mode([rejected,allowed], mode, 20, ai_only=True)], ['pam'])

    def test_ai_must_cite_nearest_favorite_and_unverified_reason_is_not_displayed(self):
        seeds = [{'article_number': 'nearest', 'title': 'PAM4 die-to-die transceiver'},
                 {'article_number': 'profile-anchor', 'title': 'Optical microring transmitter'}]
        profile = {'topics': [{'name': 'Interface', 'favorite_ids': ['profile-anchor']}]}
        candidates = [{'article_number': 'p', 'title': 'PAM4 die-to-die transceiver',
                       'related_favorite_id': 'nearest', 'shared_terms': ['die-to-die','pam','transceiver']}]
        rank = {'id': 'p', 'favorite_id': 'nearest', 'topic': 'Interface', 'score': 85,
                'novelty': 30, 'reason': 'Uses optical microring', 'evidence': 'die-to-die transceiver'}
        accepted, bad = validated_ranks({'rankings':[rank]}, candidates, profile, seeds)
        self.assertEqual(bad, 0)
        self.assertNotIn('microring', accepted['p']['ai_reason'])
        self.assertIn('die-to-die', accepted['p']['ai_reason'])
        accepted, bad = validated_ranks({'rankings':[{**rank,'favorite_id':'profile-anchor'}]}, candidates, profile, seeds)
        self.assertEqual(accepted, {})
        self.assertEqual(bad, 1)
    def test_duplicate_normalization_preserves_distinct_rates_and_serial_parts(self):
        self.assertTrue(near_duplicate('A 7-bit 150-GSa/s DAC in 5-nm FinFET CMOS', 'A 7-Bit 150-GSa/s DAC in 5nm FinFET CMOS'))
        self.assertTrue(near_duplicate('CMOS driver in 0.13&#x03BC;m technology', 'CMOS driver in 0.13 μm technology'))
        self.assertFalse(near_duplicate('A 56-Gb/s CMOS PAM4 receiver', 'A 112-Gb/s CMOS PAM4 receiver'))
        self.assertFalse(near_duplicate('The Cross-Coupled Pair - Part I', 'The Cross-Coupled Pair - Part II'))

    def test_bulk_venue_does_not_drown_independent_interests(self):
        rows = ([{'source_name': 'archive', 'title': 'packaging substrate'}] * 1400
                + [{'source_name': 'circuits', 'title': 'receiver equalizer'}] * 200
                + [{'source_name': 'conference', 'title': 'receiver equalizer'}] * 80)
        self.assertEqual(weighted_profile(rows, app._title_terms, 2), ['equalizer', 'receiver'])
        self.assertEqual(weighted_profile(list(reversed(rows)), app._title_terms), weighted_profile(rows, app._title_terms))

    def test_generic_matches_are_rejected_but_specific_favorite_is_explained(self):
        similarity = FavoriteSimilarity([
            {'article_number': 'f', 'title': 'CMOS PAM4 receiver with adaptive equalizer', 'source_name': 'JSSC'},
            {'article_number': 'g', 'title': 'Silicon photonic modulator optical link', 'source_name': 'JLT'},
        ], app._title_terms)
        result = app._rank_and_filter_candidates([
            {'title': 'CMOS digital power circuit', 'score': 100, 'year': '2026'},
            {'title': 'CMOS PAM4 receiver with equalizer', 'score': 1, 'year': '2025'},
        ], ['cmos', 'digital', 'power', 'circuit', 'pam', 'receiver', 'equalizer'], set(), 20, similarity=similarity)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['related_favorite_id'], 'f')
        self.assertGreater(result[0]['favorite_similarity'], .25)

    def test_quality_budget_does_not_fill_weak_venues(self):
        rows = [{'article_number': str(i), 'source_name': 'strong' if i < 25 else 'other',
                 'favorite_similarity': 1-i/100} for i in range(35)]
        result = allocate(rows, total=25, per_source=20)
        self.assertEqual(len(result), 25)
        self.assertEqual(sum(p['source_name'] == 'strong' for p in result), 20)
        self.assertEqual(len(allocate(rows[:3])), 3)

    def test_default_quality_budget_is_180_with_twenty_per_source(self):
        rows = [{'article_number':str(i),'source_name':f'source-{i % 12}',
                 'favorite_similarity':1-i/1000} for i in range(240)]
        result = allocate(rows)
        self.assertEqual(len(result),180)
        self.assertLessEqual(max(Counter(r['source_name'] for r in result).values()),20)

    def test_low_ai_scores_and_unassessed_candidates_cannot_fill_ai_list(self):
        rows = [{'article_number': str(i), 'source_name': 'JSSC', 'ai_score': score,
                 'year': '2026', 'ai_novelty': 100} for i, score in enumerate([25, 69, 70, 90])]
        rows.append({'article_number': 'unevaluated', 'source_name': 'JSSC', 'favorite_similarity': 1})
        for mode in ['match', 'recent', 'explore', 'diverse']:
            self.assertEqual({r['article_number'] for r in select_mode(rows, mode, 20, ai_only=True)}, {'2', '3'})
        self.assertEqual(select_mode(rows[:2], 'match', 20, ai_only=True), [])

    def test_completed_low_score_run_returns_empty_without_keyword_refill(self):
        import threading
        worker = Mock()
        service = RecommendationService(Mock(), worker, Mock())
        self.addCleanup(service.close)
        favorites = [{'article_number': 'f', 'source_name': 'JSSC', 'title': 'PAM receiver'}]
        service.snapshot = Mock(return_value={'signature': 'sig', 'favorites': favorites})
        service.profile = Mock(return_value={'topics': [{'name': 'RX', 'favorite_ids': ['f']}]})
        candidate = {'id': 1, 'article_number': 'p', 'title': 'CMOS receiver', 'source_name': 'JSSC',
                     'year': '2026', 'abstract': '', 'survey_missing': [], 'favorite_similarity': .7}
        service.sql_builder.return_value = {'items': [candidate], 'groups': [{'name': 'JSSC'}]}
        service.enrich = Mock(side_effect=deepcopy)
        worker._chat.return_value = ({'rankings': [{'id': 'p', 'score': 25, 'novelty': 100,
            'topic': 'RX', 'favorite_id': 'f', 'reason': 'General term only', 'evidence': 'CMOS receiver'}]}, {})
        with patch('local_ai_recommendations.RANKING_MODE', 'legacy'):
            result = service.run('test', {'signature': 'sig'}, threading.Event())
        self.assertEqual(result['evaluated_count'], 1)
        self.assertEqual(result['qualified_count'], 0)
        self.assertEqual(result['items'], [])

    def test_fallback_scores_do_not_depend_on_unrelated_source_position(self):
        target = {'article_number': 'a', 'source_name': 'JSSC', 'favorite_similarity': .6}
        fillers = [{'article_number': str(i), 'source_name': 'other', 'favorite_similarity': .1} for i in range(7)]
        one = select_mode([target], 'recent', 20, 2026)[0]['recommendation_score']
        many = next(r for r in select_mode(fillers+[target], 'recent', 20, 2026) if r['article_number']=='a')['recommendation_score']
        self.assertEqual(one, many)

    def test_old_policy_cache_is_not_shown_as_new_ai_recommendation(self):
        service = RecommendationService(Mock(), Mock(), Mock())
        self.addCleanup(service.close)
        service.snapshot = Mock(return_value={'signature': 'new', 'favorites': [{'article_number': 'f', 'title': 'favorite'}]})
        service.latest = Mock(side_effect=lambda signature=None, complete=False: None if signature else {
            'id': 'old', 'status': 'complete', 'request': {'signature': 'old'},
            'result': {'version': 'favorites-ai-1', 'items': [{'article_number': 'x', 'ai_score': 90}]}})
        service.schedule_refresh = Mock(return_value=True)
        service.sql_cache.get = Mock(return_value=(None, {'status': 'refreshing'}))
        service.enrich = Mock(side_effect=deepcopy)
        result = service.view()
        self.assertEqual(result['engine'], 'sql')
        self.assertEqual(result['items'], [])


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        self.addCleanup(self.engine.dispose)
        @event.listens_for(self.engine, 'before_cursor_execute', retval=True)
        def adapt(conn, cursor, statement, params, context, many):
            return statement.replace(' FOR UPDATE', ''), params
        metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            conn.execute(text('CREATE TABLE papers(article_number TEXT PRIMARY KEY,title TEXT,is_favorite INTEGER)'))
            conn.execute(text("INSERT INTO papers VALUES('p','Paper P',0),('q','Paper Q',1)"))
        patcher = patch.object(app, 'engine', self.engine)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = app.app.test_client()
        self.login('admin')
        self.headers = {'X-CSRF-Token': 'test'}

    def login(self, role):
        with self.client.session_transaction() as session:
            session.update(authenticated=True, username='test', role=role, csrf_token='test')

    def post(self, article='p', action='dismissed', headers=None):
        return self.client.post('/api/recommendations/feedback', json={'article_number': article, 'action': action},
                                headers=self.headers if headers is None else headers)

    def test_dismiss_review_restore_are_idempotent_and_preserve_favorites(self):
        for action in ['dismissed', 'dismissed', 'reviewed']:
            self.assertEqual(self.post(action=action).status_code, 200)
            data = self.client.get('/api/recommendations/feedback').json
            self.assertEqual(len(data['items']), 1)
            self.assertEqual(data['items'][0]['action'], action)
            self.assertEqual(data['items'][0]['title'], 'Paper P')
        for _ in range(2):
            self.assertEqual(self.post(action='restore').status_code, 200)
        self.assertEqual(FeedbackStore(self.engine).read()['actions'], {})
        with self.engine.connect() as conn:
            self.assertEqual(list(conn.execute(text('SELECT is_favorite FROM papers ORDER BY article_number')).scalars()), [0, 1])

    def test_permissions_invalid_actions_and_missing_papers(self):
        self.assertEqual(self.post(action='unknown').status_code, 400)
        self.assertEqual(self.post(action=[]).status_code, 400)
        self.assertEqual(self.post(article='missing').status_code, 404)
        self.assertEqual(self.post(headers={}).status_code, 403)
        self.login('viewer')
        self.assertEqual(self.post().status_code, 403)
        self.assertEqual(FeedbackStore(self.engine).read()['actions'], {})

    def test_saved_feedback_excludes_cached_ai_and_restore_reenables_it(self):
        from datetime import datetime
        service = RecommendationService(self.engine, Mock(), Mock())
        self.addCleanup(service.close)
        service.snapshot = lambda: {'signature': 'sig', 'favorites': [{'article_number': 'q', 'title': 'Paper Q'}],
                                    'feedback': FeedbackStore(self.engine).read()}
        service.latest = Mock(return_value={'id':'job','status':'complete', 'updated_at':datetime.now().isoformat(),
            'request':{'signature':'sig'}, 'result':{'version':VERSION,'items':[
                {'article_number':'p','title':'Paper P','source_name':'JSSC','ai_score':85}], 'groups':[{'name':'JSSC'}]}})
        service.enrich = Mock(side_effect=deepcopy)
        self.assertEqual(len(service.view()['items']), 1)
        self.post()
        self.assertEqual(service.view()['items'], [])
        self.post(action='restore')
        self.assertEqual(len(service.view()['items']), 1)


if __name__ == '__main__':
    unittest.main()
