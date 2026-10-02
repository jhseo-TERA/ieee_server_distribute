import copy
import json
import threading
import unittest
from unittest.mock import Mock

from local_ai_survey import SurveyAIService, build_snapshot, checked_observations, number, numeric_literals, validate_filters


def point(i, rate=10, energy=2, medium='electrical', scope='rx', **metrics):
    return {'article_number': str(i), 'title': f'Paper {i}', 'year': 2020+i,
            'venue': 'JSSC', 'link_medium': medium, 'link_subtype': 'chip_to_chip',
            'performance': {'measurement_id': i, 'lane_rate_gbps': rate,
                            'energy_pj_bit': energy, 'energy_component_scope': scope,
                            'rate_scope': 'lane', 'review_status': 'verified', **metrics}}


def dataset(points):
    ids = [p['performance']['measurement_id'] for p in points]
    return {'points': points, 'chart_sets': {key: ids for key in (
        'representative', 'lane_rate', 'lane_energy', 'reported_rate', 'reported_energy',
        'aggregate_rate', 'aggregate_energy', 'energy', 'energy_loss', 'energy_process')},
        'unmatched_points': [{'title': 'Unlinked reference'}]}


class SurveySnapshotTests(unittest.TestCase):
    def test_filters_are_strict_and_defaulted(self):
        self.assertEqual(validate_filters({})['evidence_tier'], 'structured')
        for bad in (None, [], {'medium': 'injected'}, {'sql': 'select'}, {'rate_mode': False}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_filters(bad)

    def test_pareto_is_computed_within_scope_and_medium(self):
        points = [point(1, 10, 2), point(2, 20, 1), point(3, 30, 3),
                  point(4, 100, .1, scope='tx'), point(5, 200, .1, medium='optical'),
                  point(6, 300, .01, scope='unknown')]
        summary, _, _ = build_snapshot(dataset(points), validate_filters({}))
        self.assertEqual(summary['chart_counts']['frontier'], 2)
        self.assertEqual(summary['chart_counts']['rate_energy'], 6)
        rx = next(g for g in summary['rate_energy_groups'] if g['group'] == ['electrical', 'rx', 'lane'])
        self.assertEqual((rx['n'], rx['energy_median'], rx['frontier_count']), (3, 2, 2))
        frontier, _, _ = build_snapshot(dataset(points), validate_filters({'pareto_mode': 'frontier'}))
        self.assertEqual(frontier['chart_counts']['rate_energy'], 2)
        self.assertEqual(frontier['chart_counts']['rate_year'], 6)

    def test_medium_and_subtype_filter_all_chart_sets(self):
        points = [point(1), point(2, medium='optical')]
        summary, rows, _ = build_snapshot(dataset(points), validate_filters({'medium': 'optical'}))
        self.assertEqual([p['article_number'] for p in rows], ['2'])
        self.assertEqual(summary['chart_counts']['rate_year'], 1)
        summary, _, _ = build_snapshot(dataset(points), validate_filters({'subtype': 'die_to_die'}))
        self.assertEqual(summary['matched_papers'], 0)

    def test_same_point_pairs_are_not_fabricated(self):
        data = dataset([point(1, 10, None), point(2, None, 2)])
        data['points'][1]['article_number'] = '1'
        summary, _, _ = build_snapshot(data, validate_filters({}))
        self.assertEqual(summary['matched_papers'], 1)
        self.assertEqual(summary['chart_counts']['rate_energy'], 0)

    def test_rate_modes_and_reported_denominators_do_not_mix(self):
        points = [point(i, reported_rate_gbps=100+i, aggregate_rate_gbps=400+i) for i in range(1, 4)]
        points[2]['performance']['rate_scope'] = 'aggregate'
        summary, _, _ = build_snapshot(dataset(points), validate_filters({'rate_mode': 'reported'}))
        self.assertEqual(summary['chart_counts']['frontier'], 0)
        self.assertEqual(len(summary['rate_energy_groups']), 2)
        summary, _, _ = build_snapshot(dataset(points), validate_filters({'rate_mode': 'aggregate'}))
        self.assertEqual(summary['chart_counts']['frontier'], 1)

    def test_chart_sets_are_authoritative_for_scope(self):
        data = dataset([point(1), point(2, scope='tx')])
        data['chart_sets']['energy'] = [1]
        summary, _, _ = build_snapshot(data, validate_filters({'energy_scope': 'rx'}))
        self.assertEqual(summary['chart_counts']['energy_year'], 1)
        self.assertEqual(summary['chart_counts']['rate_year'], 2)

    def test_zero_loss_and_invalid_numeric_values_match_chart_rules(self):
        data = dataset([point(1, channel_loss_db=0, process_nm=0), point(2, energy=float('inf'))])
        summary, _, _ = build_snapshot(data, validate_filters({}))
        self.assertEqual(summary['chart_counts']['energy_loss'], 1)
        self.assertEqual(summary['chart_counts']['energy_process'], 0)
        for value in (True, None, '', float('nan'), float('inf'), 0, -1):
            self.assertIsNone(number(value))

    def test_sample_is_bounded_and_not_counted_as_corpus(self):
        summary, points, sample = build_snapshot(dataset([point(i) for i in range(1, 31)]), validate_filters({}))
        self.assertEqual(summary['matched_papers'], 30)
        self.assertEqual(len(sample), 12)
        self.assertEqual(len({p['performance']['measurement_id'] for p in sample}), 12)
        self.assertEqual(summary['unmatched_reference_excluded'], 1)


class SurveyServiceTests(unittest.TestCase):
    def setUp(self):
        self.worker = Mock()
        self.worker.enqueue.side_effect = lambda req, owner: {'request': req, 'owner': owner}
        self.data = dataset([point(1), point(2), point(3, medium='optical')])
        self.builder = Mock(return_value=self.data)
        self.ai = SurveyAIService(self.worker, self.builder)
        self.worker.repo.get_papers.side_effect = lambda keys: [
            {'article_number': k, 'title': f'Paper {k}', 'year': 2020, 'source_system': 'ieee',
             'pdf_available': 1, 'is_favorite': 1, 'abstract_text': 'Stored abstract.'} for k in keys]

    def test_analyze_captures_server_snapshot_without_pdf_or_source_mutation(self):
        job = self.ai.submit({'kind': 'analyze'}, 'viewer', 'viewer')
        self.assertEqual(job['owner'], 'viewer')
        req = job['request']
        self.assertEqual((req['kind'], req['model']), ('survey_analyze', 'gpt-oss:20b'))
        self.assertEqual(req['survey_snapshot']['summary']['matched_papers'], 3)
        self.builder.assert_called_once_with(validate_filters({}), strict=True)
        self.worker.docs.assert_not_called()
        self.worker.repo.decide_proposals.assert_not_called()

    def test_payload_cannot_supply_metrics_or_model(self):
        for bad in ({'summary': {'count': 99}}, {'model': 'evil'}, {'kind': []},
                    {'kind': 'compare', 'article_numbers': ['1']},
                    {'kind': 'compare', 'article_numbers': ['1', '1']},
                    {'kind': 'compare', 'article_numbers': ['1'] * 6},
                    {'kind': 'analyze', 'article_numbers': ['1']},
                    {'question': 'x' * 2001}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.ai.submit(bad, 'a', 'viewer')
        self.worker.enqueue.assert_not_called()

    def test_empty_analysis_rejected_without_queuing(self):
        with self.assertRaises(ValueError):
            self.ai.submit({'filters': {'medium': 'unspecified'}}, 'a', 'viewer')
        self.worker.enqueue.assert_not_called()

    def test_compare_warns_for_paper_outside_filter_and_never_reads_pdf(self):
        job = self.ai.submit({'kind': 'compare', 'article_numbers': ['1', '3'], 'filters': {'medium': 'electrical'}}, 'a', 'viewer')
        snap = job['request']['survey_snapshot']
        self.assertEqual(len(snap['comparison'][0]['matching_points']), 1)
        self.assertEqual(snap['comparison'][1]['matching_points'], [])
        self.assertTrue(any('3:' in w for w in snap['warnings']))
        self.worker.docs.render_page.assert_not_called()

    def test_compare_does_not_label_off_scope_energy_as_matching(self):
        self.data['points'][0]['performance']['energy_component_scope'] = 'tx'
        job = self.ai.submit({'kind': 'compare', 'article_numbers': ['1', '2'], 'filters': {'energy_scope': 'rx'}}, 'a', 'viewer')
        self.assertEqual(job['request']['survey_snapshot']['comparison'][0]['matching_points'], [])

    def test_nonexistent_or_non_ieee_paper_rejected(self):
        self.worker.repo.get_papers.side_effect = None
        for papers in ([], [{'source_system': 'other'}, {'source_system': 'ieee'}]):
            self.worker.repo.get_papers.return_value = papers
            with self.assertRaises(ValueError):
                self.ai.submit({'kind': 'compare', 'article_numbers': ['1', '2']}, 'a', 'viewer')

    def test_run_uses_saved_snapshot_and_marks_unverified_interpretation(self):
        req = self.ai.submit({}, 'a', 'viewer')['request']
        expected = copy.deepcopy(req['survey_snapshot']['summary'])
        self.data['points'].clear()
        self.worker._chat.return_value = ({'observations': [
            {'statement': '연결 논문 수는 3편입니다.', 'source_id': 'S1', 'evidence_quote': '"matched_papers": 3'},
            {'statement': '가짜 출처입니다.', 'source_id': 'S999', 'evidence_quote': 'not a source'}], 'limitations': []}, {})
        result = self.ai.run('job', req, threading.Event())
        self.assertEqual(result['summary'], expected)
        self.assertEqual(result['citations'], ['S1'])
        self.assertEqual(result['grounding'], 'needs_review')
        self.assertEqual(result['validation']['rejected'], 1)
        self.assertTrue(any('표시하지 않았습니다' in w for w in result['limitations']))
        self.assertEqual(self.builder.call_count, 1)

    def test_gap_queue_merges_conflicts_and_keeps_favorites(self):
        self.worker.repo.review_queue.return_value = {
            'review_queue': [{'article_number': '1', 'priority': 'P0', 'flag_codes': ['conflict']}],
            'missing_fields': [{'article_number': '1', 'missing_fields': ['loss']},
                               {'article_number': '2', 'missing_fields': ['energy']}],
        }
        result = self.ai.gaps()
        self.assertEqual(result['returned_count'], 2)
        self.assertEqual(result['items'][0]['priority'], 'P0')
        self.assertEqual(result['items'][0]['missing_fields'], ['loss'])
        self.assertTrue(all(p['is_favorite'] and p['pdf_available'] for p in result['items']))
        self.worker._chat.assert_not_called()

    def test_statement_requires_real_excerpt_and_no_new_numbers(self):
        sources = [{'source_id': 'S1', 'text': 'Measured rate 112 Gb/s and energy 2.0 pJ/bit.'}]
        good = {'source_id': 'S1', 'statement': '보고 속도 112 Gb/s, 에너지 2 pJ/bit입니다.', 'evidence_quote': 'rate 112 Gb/s and energy 2.0 pJ/bit'}
        invalid = [dict(good, statement='속도는 224 Gb/s입니다.'),
                   dict(good, evidence_quote='rate 112 Gb/s and energy 1.0'),
                   dict(good, statement='Need JSON with answer field. Ensure proper.'),
                   dict(good, statement='에너지 2 pJ/bit [S2] 입니다.')]
        accepted, rejected = checked_observations({'observations': [good, *invalid]}, sources)
        self.assertEqual(accepted, [good])
        self.assertEqual(rejected, 4)
        self.assertEqual(numeric_literals('1,000.0 and 1.0e-12'), {1000, 1e-12})

    def test_unverifiable_response_retries_once_then_fails_closed(self):
        req = self.ai.submit({}, 'a', 'viewer')['request']
        self.worker._chat.return_value = ({'observations': [{'source_id': 'S999', 'statement': '이것은 확인 불가입니다.', 'evidence_quote': 'invented quote'}], 'limitations': []}, {})
        with self.assertRaises(ValueError):
            self.ai.run('job', req, threading.Event())
        self.assertEqual(self.worker._chat.call_count, 2)

    def test_incomplete_json_can_retry_with_a_short_grounded_response(self):
        req = self.ai.submit({}, 'a', 'viewer')['request']
        self.worker._chat.side_effect = [ValueError('incomplete JSON'), ({'observations': [
            {'statement': '연결 논문 수는 3편입니다.', 'source_id': 'S1', 'evidence_quote': '"matched_papers": 3'}], 'limitations': []}, {})]
        result = self.ai.run('job', req, threading.Event())
        self.assertEqual(result['validation']['attempts'], 2)

    def test_evidence_index_cannot_cross_groups_or_add_numeric_claims(self):
        sources = [{'source_id': 'S1', 'text': 'Full raw statistics', 'evidence_lines': [
            'Group RX: energy_median=2.0 pJ/bit; rate_max=112 Gb/s.',
            'Group TX: energy_median=1.0 pJ/bit; rate_max=224 Gb/s.']}]
        good = {'source_id': 'S1', 'evidence_index': 0, 'statement': 'RX 에너지 중앙값은 2 pJ/bit입니다.'}
        bad = [dict(good, evidence_index=8), dict(good, evidence_index=True),
               dict(good, statement='RX 속도는 224 Gb/s입니다.'),
               dict(good, statement='평균 에너지는 2 pJ/bit입니다.'),
               dict(good, statement='가장 많은 연구가 RX에 집중되어 있습니다.')]
        accepted, rejected = checked_observations({'observations': [good, *bad]}, sources)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]['evidence_quote'], sources[0]['evidence_lines'][0])
        self.assertEqual(rejected, 5)

    def test_snapshot_has_bounded_server_selected_evidence_lines(self):
        req = self.ai.submit({}, 'a', 'viewer')['request']
        sources = req['survey_snapshot']['sources']
        self.assertIn('서버 SQL 집계', sources[0]['evidence_lines'][0])
        self.assertIn('measurement_id=1', next(s for s in sources if s.get('article_number') == '1')['evidence_lines'][0])
        self.assertTrue(all(len(s['evidence_lines']) <= 21 for s in sources))

    def test_counts_cannot_be_reinterpreted_as_physical_quantities(self):
        sources = [{'source_id': 'S1', 'evidence_lines': ['속도-연도 차트 성능점 233개입니다.']}]
        accepted, rejected = checked_observations({'observations': [
            {'source_id': 'S1', 'evidence_index': 0, 'statement': '속도는 233 Gb/s입니다.'}]}, sources)
        self.assertEqual((accepted, rejected), ([], 1))

    def test_statistics_keep_server_wording_even_if_numbers_match(self):
        line = '모든 차트 연결 논문 780편, 속도-연도 성능점 233개입니다.'
        sources = [{'source_id': 'S1', 'kind': 'survey_statistics', 'evidence_lines': [line]}]
        accepted, rejected = checked_observations({'observations': [
            {'source_id': 'S1', 'evidence_index': 0, 'statement': '속도-연도 차트 논문 780편이 성능점 233개를 제공합니다.'}]}, sources)
        self.assertEqual(rejected, 0)
        self.assertEqual(accepted[0]['statement'], line)
        self.assertEqual(accepted[0]['basis'], 'server_statistics')


if __name__ == '__main__':
    unittest.main()
