"""Whole-corpus retrieval, account masks and broad-pool presentation tests."""
from copy import deepcopy
from datetime import datetime
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, text
import web.app as app
from paper_server.accounts import account_scope
from paper_server.ai.recommendations import RecommendationService, select_sql_mode
from paper_server.recommendation.retrieval import TitleIndex, TitleIndexCache, retrieve_corpus_candidates


def paper(article, title, source='JSSC', year='2025'):
    return {'id': int(article), 'article_number': str(article), 'title': title, 'source_name': source, 'year': year}


class WholeCorpusTests(unittest.TestCase):
    def build(self, rows):
        return TitleIndex(rows, app._title_terms, app._is_low_quality_title)

    def test_single_minority_favorite_retrieves_outside_dominant_profile(self):
        favorites = [paper(i, f'{i} Gb/s PAM4 receiver with DFE') for i in range(1, 30)]
        rare = paper(30, 'Brillouin acoustic waveguide resonator', 'OFC')
        favorites.append(rare)
        rows = favorites + [paper(100, 'Stimulated Brillouin acoustic waveguide scattering', 'JLT'),
                            paper(101, 'Adaptive PAM4 receiver with DFE'),
                            paper(102, 'CMOS digital power integrated circuits')]
        result, stats = self.build(rows).retrieve(favorites, current_year=2026)
        rare_match = next(row for row in result if row['article_number'] == '100')
        self.assertEqual(rare_match['related_favorite_id'], '30')
        self.assertEqual(stats['searched_favorites'], 30)
        self.assertNotIn('102', {row['article_number'] for row in result})
        # A user's evidence-backed interest need not fit seven hard-coded labels.
        visible = select_sql_mode(result, 'diverse', 20)
        self.assertIn('100', {row['article_number'] for row in visible})

    def test_every_favorite_gets_a_turn_before_more_from_dominant_interest(self):
        favorites = [paper(1, 'PAM4 receiver DFE'), paper(2, 'Brillouin acoustic waveguide resonator')]
        rows = favorites + [paper(i, f'{i} Gb/s PAM4 receiver DFE adaptive') for i in range(10, 50)]
        rows += [paper(100, 'Stimulated Brillouin acoustic waveguide scattering')]
        result, _ = self.build(rows).retrieve(favorites, limit=2)
        self.assertEqual(len(result), 2)
        self.assertIn('100', {r['article_number'] for r in result})

    def test_shared_index_does_not_share_personal_exclusions_or_favorites(self):
        rows = [paper(1, 'PAM4 receiver DFE'), paper(2, 'Optical microring modulator'),
                paper(3, 'Adaptive PAM4 receiver with DFE'), paper(4, 'Tunable optical microring modulator')]
        index = self.build(rows)
        before = deepcopy(rows)
        a, _ = index.retrieve(rows[:1], excluded=['3'])
        b, _ = index.retrieve(rows[1:2])
        again, _ = index.retrieve(rows[:1])
        self.assertNotIn('3', {r['article_number'] for r in a})
        self.assertIn('4', {r['article_number'] for r in b})
        self.assertIn('3', {r['article_number'] for r in again})
        self.assertEqual(rows, before)

    def test_saved_paper_duplicates_feedback_corrections_and_explicit_exclusions_stay_out(self):
        favorite = paper(1, '56-Gb/s PAM4 wireline receiver DFE')
        rows = [favorite, paper(2, '56-Gb/s PAM4 wireline receiver DFE'),
                paper(3, 'Publisher Correction: 56-Gb/s PAM4 wireline receiver DFE'),
                paper(4, 'DMT wireline receiver with PAM4 comparison circuit'),
                paper(5, 'PAM4 wireline receiver DFE adaptation'),
                paper(6, '112-Gb/s PAM4 wireline receiver DFE')]
        # Use an unambiguous excluded title; comparisons are intentionally kept.
        rows[3]['title'] = 'DMT wireline receiver with DFE'
        result, _ = self.build(rows).retrieve([favorite], excluded=['5'])
        self.assertEqual({r['article_number'] for r in result}, {'6'})

    def test_alias_matching_and_literal_evidence_remain_distinct(self):
        rows = [paper(1, 'Optical receiver transimpedance amplifier'), paper(2, 'Optical receiver TIA with feedback')]
        result, _ = self.build(rows).retrieve(rows[:1])
        self.assertEqual(len(result), 1)
        self.assertIn('tia', result[0]['shared_concepts'])
        self.assertNotIn('tia', result[0]['shared_terms'])

    def test_deterministic_order_and_empty_inputs(self):
        rows = [paper(1, 'PAM4 receiver DFE'), paper(2, 'Adaptive PAM4 receiver DFE'),
                paper(3, 'PAM4 receiver DFE adaptive')]
        a, _ = self.build(rows).retrieve(rows[:1])
        b, _ = self.build(list(reversed(rows))).retrieve(rows[:1])
        self.assertEqual(a, b)
        self.assertEqual(self.build(rows).retrieve([])[0], [])
        self.assertEqual(self.build(rows).retrieve(rows[:1], limit=0)[0], [])
        self.assertEqual(self.build([]).retrieve(rows[:1])[0], [])

    def test_cache_reuses_only_corpus_and_refreshes_after_change_or_expiry(self):
        cache = TitleIndexCache(ttl=60)
        loader = Mock(return_value=[paper(1, 'PAM4 receiver DFE')])
        first = cache.get('one', loader, app._title_terms, app._is_low_quality_title)
        self.assertIs(first, cache.get('one', loader, app._title_terms, app._is_low_quality_title))
        self.assertEqual(loader.call_count, 1)
        self.assertIsNot(first, cache.get('two', loader, app._title_terms, app._is_low_quality_title))
        cache.created -= 61
        cache.get('two', loader, app._title_terms, app._is_low_quality_title)
        self.assertEqual(loader.call_count, 3)

    def test_exploration_accepts_adjacent_neighbors_without_weakening_match_mode(self):
        row = {**paper(1, 'Optical microring modulator'), 'favorite_similarity': .22,
               'retrieval_basis': 'favorite-neighbors-1', 'related_favorite_id': 'f',
               'shared_technical_terms': ['microring']}
        self.assertEqual(select_sql_mode([row], 'match', 20), [])
        self.assertEqual(len(select_sql_mode([row], 'explore', 20)), 1)

    def test_recent_lane_reaches_beyond_the_closest_neighbors(self):
        favorite = paper(1, 'PAM4 receiver DFE adaptive')
        rows = [favorite] + [paper(i, f'{i} Gb/s PAM4 receiver DFE adaptive', year='2010') for i in range(10, 40)]
        rows += [paper(100, 'PAM4 receiver DFE', year='2026')]
        result, _ = self.build(rows).retrieve([favorite], current_year=2026)
        newest = next(r for r in result if r['article_number'] == '100')
        self.assertIn('recent', newest['retrieval_channels'])

    def test_series_label_alone_does_not_establish_shared_technical_interest(self):
        rows = [paper(1, 'The Ring Oscillator [A Circuit for All Seasons]'),
                paper(2, 'The Bandgap Reference [A Circuit for All Seasons]')]
        self.assertEqual(self.build(rows).retrieve(rows[:1])[0], [])

    def test_duplicate_candidates_across_sources_do_not_consume_display_slots(self):
        rows = [paper(1, 'PAM4 receiver DFE'), paper(2, 'Adaptive PAM4 receiver DFE', 'JSSC'),
                paper(3, 'Adaptive PAM4 receiver DFE', 'ISSCC'), paper(4, 'PAM4 receiver DFE with calibration')]
        result, _ = self.build(rows).retrieve(rows[:1], limit=2)
        self.assertEqual(len(result), 2)
        self.assertEqual(len({r['title'] for r in result}), 2)


class PoolPresentationTests(unittest.TestCase):
    def test_normal_modes_enrich_only_the_displayed_papers(self):
        service = RecommendationService(Mock(), None, Mock())
        self.addCleanup(service.close)
        rows = [{**paper(i, 'PAM4 receiver DFE'), 'favorite_similarity': .8} for i in range(1, 301)]
        service.snapshot = Mock(return_value={'signature': 's', 'favorites': [paper(500, 'favorite')], 'feedback': {}})
        service.sql_cache.get = Mock(return_value=({'items': rows, 'groups': [{'name': 'JSSC'}]}, {'status': 'ready'}))
        service.enrich = Mock(side_effect=lambda data: data)
        result = service.view('match', 20)
        self.assertEqual(len(service.enrich.call_args.args[0]), 20)
        self.assertEqual(len(result['items']), 20)

    def test_sql_recheck_uses_each_accounts_favorites(self):
        engine = create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        with engine.begin() as conn:
            conn.execute(text('CREATE TABLE paper_current_abstracts(paper_id INT,abstract_text TEXT,is_current INT)'))
            conn.execute(text('CREATE TABLE accounts(id INT,role TEXT,is_active INT,favorites_source_account_id INT)'))
            conn.execute(text("INSERT INTO accounts VALUES (1,'member',1,NULL),(2,'member',1,NULL)"))
            conn.execute(text('CREATE TABLE account_favorites(account_id INT,paper_id INT)'))
            conn.execute(text('CREATE TABLE papers(id INT,article_number TEXT,title TEXT,source_name TEXT,year TEXT,'
                'authors TEXT,source_type TEXT,source_system TEXT,issue TEXT,url TEXT,pdf_available INT,is_favorite INT)'))
            for row in [paper(1, 'PAM4 receiver DFE'), paper(2, 'Adaptive PAM4 receiver DFE')]:
                conn.execute(text('INSERT INTO papers(id,article_number,title,source_name,year) VALUES (:id,:article_number,:title,:source_name,:year)'), row)
            conn.execute(text('INSERT INTO account_favorites VALUES (1,1),(1,2),(2,1)'))
            conn.execute(text("INSERT INTO paper_current_abstracts VALUES (2,'A receiver using inductive peaking.',1)"))
        with patch('paper_server.recommendation.retrieval._CORPUS_INDEX', TitleIndexCache()):
            # Deliberately stale seed list: current SQL must still hide paper 2
            # for account 1 without hiding it for account 2.
            for account, expected in [(1, []), (2, ['2'])]:
                with account_scope(account):
                    rows, _ = retrieve_corpus_candidates(engine, [paper(1, 'PAM4 receiver DFE')], [],
                                                         app._title_terms, app._is_low_quality_title, 2026)
                self.assertEqual([row['article_number'] for row in rows], expected)


if __name__ == '__main__':
    unittest.main()
