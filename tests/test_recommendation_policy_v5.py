"""Offline relevance and mode regressions; no DB or model access required."""
from copy import deepcopy
from datetime import date
import unittest
from unittest.mock import Mock

import web.app as app
from paper_server.ai.recommendations import select_mode, select_sql_mode
from paper_server.recommendation.policy import (
    FavoriteSimilarity, MIN_SIMILARITY, diverse_select, expanded_search_terms,
    technical_terms, classify_interest_topic,
)


ALIAS_PAIRS = [
    ('PAM4 Clock and Data Recovery for Wireline Links', 'PAM-4 CDR for Wireline Links'),
    ('PAM4 Receiver with Decision-Feedback Equalization', 'PAM-4 Receiver with DFE'),
    ('PAM4 Receiver with Feed-Forward Equalizer', 'PAM4 Receiver with FFE'),
    ('Optical Receiver with Transimpedance Amplifier', 'Optical Receiver with TIA'),
    ('Clock Synthesis Using a Phase-Locked Loop', 'Clock Synthesis Using a PLL'),
    ('High Bandwidth Memory Interface with Crosstalk Cancellation', 'HBM Interface with Crosstalk Cancellation'),
    ('Wavelength Division Multiplexing Optical Transceiver', 'WDM Optical Transceiver'),
]


class SemanticRelevanceTests(unittest.TestCase):
    def test_abbreviation_and_expansion_find_the_same_favorite(self):
        for favorite, candidate in ALIAS_PAIRS:
            with self.subTest(candidate=candidate):
                similarity = FavoriteSimilarity([
                    {'article_number': 'f', 'source_name': 'JSSC', 'title': favorite},
                    {'article_number': 'other', 'source_name': 'JLT', 'title': 'Laser fabrication cavity temperature'},
                ], app._title_terms)
                evidence = similarity.evidence(candidate)
                self.assertGreaterEqual(evidence['favorite_similarity'], MIN_SIMILARITY)
                self.assertEqual(evidence['related_favorite_id'], 'f')
                self.assertTrue(evidence['shared_concepts'])

    def test_generic_words_do_not_create_technical_relevance(self):
        model = FavoriteSimilarity([{'article_number': 'f', 'title': 'Integrated CMOS optical receiver'}], app._title_terms)
        for title in ['Low power CMOS optical receiver', 'Integrated digital CMOS power circuit']:
            self.assertLess(model.evidence(title)['favorite_similarity'], MIN_SIMILARITY)

    def test_pam_orders_remain_distinct_in_scoring(self):
        model = FavoriteSimilarity([{'article_number': 'f', 'title': 'PAM4 receiver with DFE'}], app._title_terms)
        matched = model.evidence('PAM-4 receiver with DFE')
        different = model.evidence('PAM-2 receiver with DFE')
        self.assertGreater(matched['favorite_similarity'], different['favorite_similarity'])
        self.assertNotIn('pam', technical_terms('PAM-4 receiver', app._title_terms))

    def test_aliases_do_not_invent_literal_evidence(self):
        model = FavoriteSimilarity([{'article_number': 'f', 'title': ALIAS_PAIRS[3][0]}], app._title_terms)
        evidence = model.evidence(ALIAS_PAIRS[3][1])
        self.assertIn('tia', evidence['shared_concepts'])
        self.assertNotIn('tia', evidence['shared_terms'])

    def test_topic_labels_are_consistent_for_abbreviations(self):
        for first, second in ALIAS_PAIRS + [('Capacitive Die-to-Die', 'Capacitive D2D')]:
            with self.subTest(title=first):
                self.assertEqual(classify_interest_topic({'title': first}), classify_interest_topic({'title': second}))
                self.assertNotEqual(classify_interest_topic({'title': second}), '기타 관련 연구')

    def test_rare_interest_is_not_rejected_by_global_profile_overlap(self):
        model = FavoriteSimilarity([{'article_number': 'f', 'title': 'Optical Receiver with Transimpedance Amplifier'}], app._title_terms)
        rows = [{'article_number': 'p', 'title': 'Optical Receiver with TIA', 'score': 1}]
        selected = app._rank_and_filter_candidates(rows, ['pam', 'wireline', 'equalizer'], set(), 20, similarity=model)
        self.assertEqual([r['article_number'] for r in selected], ['p'])

    def test_repeated_imports_do_not_change_similarity(self):
        favorites = [{'article_number': 'f', 'source_name': 'JSSC', 'title': 'PAM4 receiver with DFE'},
                     {'article_number': 'g', 'source_name': 'JLT', 'title': 'Optical microring modulator'}]
        candidate = 'PAM4 receiver with adaptive DFE and equalizer'
        one = FavoriteSimilarity(favorites, app._title_terms).evidence(candidate)
        many = FavoriteSimilarity(favorites + favorites[:1] * 100, app._title_terms).evidence(candidate)
        self.assertEqual(one, many)

    def test_exact_short_duplicate_is_detected_without_three_shared_words(self):
        model = FavoriteSimilarity([{'article_number': 'f', 'title': 'PAM4 receiver'}], app._title_terms)
        self.assertTrue(model.evidence('PAM4 receiver')['near_duplicate_favorite'])

    def test_duplicate_identity_with_different_punctuation_keeps_all_terms(self):
        favorites = [{'article_number': 'a', 'title': 'PAM4 receiver with SAR-ADC'},
                     {'article_number': 'b', 'title': 'PAM4 receiver with SAR ADC'}]
        model = FavoriteSimilarity(favorites, app._title_terms)
        evidence = model.evidence('PAM4 receiver using SAR-ADC and DFE')
        self.assertGreaterEqual(evidence['favorite_similarity'], MIN_SIMILARITY)
        self.assertEqual(evidence, FavoriteSimilarity(list(reversed(favorites)), app._title_terms).evidence(
            'PAM4 receiver using SAR-ADC and DFE'))

    def test_retrieval_expands_both_directions_without_unrelated_aliases(self):
        self.assertIn('transimpedance', expanded_search_terms(['tia', 'receiver']))
        self.assertIn('tia', expanded_search_terms(['transimpedance', 'amplifier']))
        self.assertIn('dfe', expanded_search_terms(['decision-feedback', 'equalizer']))
        self.assertNotIn('hbm', expanded_search_terms(['optical', 'receiver']))

    def test_sql_candidate_query_uses_expansion_and_stable_ties(self):
        connection = Mock()
        connection.execute.return_value.mappings.return_value.all.return_value = []
        app._search_source(connection, 'JSSC', ['tia', 'receiver'], [], 20, 0, set(), date(2026, 10, 11))
        statement, params = connection.execute.call_args.args
        self.assertIn('transimpedance', params['global_q'])
        self.assertIn('article_number ASC', str(statement))


class RecommendationModeTests(unittest.TestCase):
    def test_exploration_does_not_promote_weak_uncollected_topic(self):
        rows = [
            {'article_number': 'fit', 'source_name': 'JSSC', 'title': 'PAM4 receiver', 'favorite_similarity': .70},
            {'article_number': 'weak', 'source_name': 'JSSC', 'title': 'Optical microring modulator', 'favorite_similarity': .25},
        ]
        result = select_sql_mode(rows, 'explore', 1, {'수신기': 100, '광 I/O': 0}, 2026)
        self.assertEqual(result[0]['article_number'], 'fit')

    def test_exploration_still_surfaces_relevant_under_collected_topic(self):
        rows = [
            {'article_number': 'fit', 'source_name': 'JSSC', 'title': 'PAM4 receiver', 'favorite_similarity': .9},
            {'article_number': 'adjacent', 'source_name': 'JSSC', 'title': 'Optical microring modulator', 'favorite_similarity': .75},
        ]
        counts = {'수신기': 100, '광 I/O': 1}
        self.assertEqual(select_sql_mode(rows, 'match', 1, counts)[0]['article_number'], 'fit')
        self.assertEqual(select_sql_mode(rows, 'explore', 1, counts)[0]['article_number'], 'adjacent')

    def test_recent_does_not_let_date_alone_rescue_weak_match(self):
        rows = [
            {'article_number': 'fit', 'source_name': 'JSSC', 'title': 'PAM4 receiver', 'favorite_similarity': .9, 'year': '2010'},
            {'article_number': 'weak', 'source_name': 'JSSC', 'title': 'PAM4 receiver', 'favorite_similarity': .25, 'year': '2026'},
        ]
        self.assertEqual(select_sql_mode(rows, 'recent', 1, current_year=2026)[0]['article_number'], 'fit')

    def test_diversity_detects_repeated_content_with_different_topic_labels(self):
        rows = [
            {'article_number': 'a', 'source_name': 'JSSC', 'title': 'PAM4 receiver with DFE', 'ai_score': 95, 'ai_topic': 'RX'},
            {'article_number': 'b', 'source_name': 'JSSC', 'title': 'PAM-4 receiver with Decision-Feedback Equalization', 'ai_score': 94, 'ai_topic': 'EQ'},
            {'article_number': 'c', 'source_name': 'JSSC', 'title': 'Optical microring modulator with wavelength tuning', 'ai_score': 88, 'ai_topic': 'TX'},
        ]
        before = deepcopy(rows)
        selected = select_mode(rows, 'diverse', 2)
        self.assertEqual([r['article_number'] for r in selected], ['a', 'c'])
        self.assertEqual(selected, select_mode(list(reversed(rows)), 'diverse', 2))
        self.assertEqual(before, rows)

    def test_diversity_is_global_across_sources_and_preserves_caps(self):
        rows = [
            {'article_number': 'a', 'source_name': 'A', 'title': 'PAM4 receiver with DFE', 'recommendation_score': 95},
            {'article_number': 'b', 'source_name': 'B', 'title': 'PAM-4 receiver with DFE', 'recommendation_score': 94},
            {'article_number': 'c', 'source_name': 'B', 'title': 'Optical microring modulator', 'recommendation_score': 88},
        ]
        selected = diverse_select(rows, 1, total=2)
        self.assertEqual([r['article_number'] for r in selected], ['a', 'c'])
        self.assertEqual(diverse_select(rows, 0), [])


if __name__ == '__main__':
    unittest.main()
