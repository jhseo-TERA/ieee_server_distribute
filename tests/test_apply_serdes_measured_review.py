"""Transactional import guards against small synthetic, non-personal fixtures."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts import apply_serdes_measured_review as apply


TABLES = (
    'serdes_measurements', 'serdes_implementations', 'serdes_implementation_papers',
    'serdes_measurement_evidence', 'serdes_paper_screenings', 'serdes_screening_runs',
    'serdes_implementation_families', 'serdes_implementation_family_members',
    'serdes_implementation_match_candidates', 'serdes_implementation_match_decision_events',
    'serdes_measurement_scope_overrides', 'serdes_paper_subtype_overrides',
    'serdes_paper_bibliography', 'serdes_measurement_metric_scopes',
    'serdes_paper_link_media', 'serdes_paper_link_subtypes',
)


class MeasuredReviewApplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.original = {'papers': [], 'tables': {name: [] for name in TABLES}}
        self.qualified = {'rows': []}
        sources = []
        for pid, article, source in ((1, '1001', 'ieee'), (2, 'OFC-2026-Test.1', 'optica'),
                                     (3, '9830507', 'ieee')):
            path = self.directory / f'{pid}.pdf'
            path.write_bytes(f'%PDF-1.4\nSynthetic source {pid}\nMeasured test fixture'.encode())
            sha = apply.digest(path.read_bytes())
            venue = 'OFC' if source == 'optica' else 'JSSC'
            self.original['papers'].append(dict(id=pid, article_number=article,
                                                 source_system=source, source_name=venue))
            self.qualified['rows'].append(dict(paper_id=pid, article=article,
                                               source_system=source, verdict='기존 보강' if pid != 2 else '반영 후보',
                                               pdf_sha256=sha, pdf_path=str(path)))
            sources.append(dict(paper_id=pid, path=str(path), sha256=sha))
        self.original['tables']['serdes_implementations'] = [
            dict(id=10, implementation_key='ieee:1001', canonical_paper_id=1),
            dict(id=30, implementation_key='ieee:9830507', canonical_paper_id=3),
        ]
        self.original['tables']['serdes_implementation_papers'] = [
            dict(implementation_id=10, paper_id=1, relation_type='primary'),
            dict(implementation_id=30, paper_id=3, relation_type='primary'),
        ]
        self.original['tables']['serdes_measurements'] = [
            dict(id=11, implementation_id=10, operating_point_key='existing',
                 lane_rate_gbps='56', process_nm=None, process_text=None, lane_count=None,
                 power_mw=None, energy_pj_bit=None, ber=None,
                 review_status='extracted', source_kind='abstract', updated_at='before'),
            dict(id=31, implementation_id=30, operating_point_key='held',
                 lane_rate_gbps='16', process_nm='28', energy_pj_bit='2.02',
                 review_status='reviewed', source_kind='pdf', updated_at='before'),
        ]
        self.original['tables']['serdes_paper_screenings'] = [
            self.screen(1), self.screen(3), self.screen(4, include=0),
        ]
        self.original['tables']['serdes_screening_runs'] = [
            dict(id=100, venue='JSSC', status='complete', scope_name='all')]
        self.original['tables']['serdes_implementation_families'] = [dict(id=50, label='untouched')]
        self.original['tables']['serdes_measurement_metric_scopes'] = [
            dict(measurement_id=31, energy_component_scope='unknown')]
        self.original['tables']['serdes_paper_bibliography'] = [dict(paper_id=4, authors='Untouched')]
        t1 = dict(paper_id=1, screening=self.screen(1), implementation_id=10,
                  updates=[dict(measurement_id=11, fields={'process_nm': '28'})],
                  new_points=[], evidence=[self.evidence(1, 'process_nm', 11)])
        values = {c: None for c in apply.MEASUREMENT_COLUMNS}
        values.update(operating_point_key='pdf-reviewed-56', component_scope='tx',
                      lane_rate_gbps='56', lane_count=1, source_kind='pdf',
                      rate_scope='lane', review_status='reviewed', overall_confidence='0.9',
                      power_scope='unknown', energy_scope='unknown', ber_scope='unknown')
        point = dict(values=values, evidence=[self.evidence(2, 'lane_rate_gbps'),
                                             self.evidence(2, 'lane_count')],
                     energy_component_scope='unknown')
        t2 = dict(paper_id=2, screening=self.screen(2, venue='OFC'), implementation_id=None,
                  implementation=dict(implementation_key='optica:OFC-2026-Test.1', canonical_paper_id=2),
                  updates=[], new_points=[point], evidence=[])
        t3 = dict(paper_id=3, screening=self.screen(3), implementation_id=30,
                  updates=[], new_points=[], evidence=[])
        self.plan = dict(version=apply.VERSION, targets=[t1, t2, t3], sources=sources,
                         before_fingerprints={k: apply.fingerprint(v) for k, v in self.original['tables'].items()},
                         summary={'papers_included': 3})

    @staticmethod
    def screen(pid, venue='JSSC', include=1):
        return dict(paper_id=pid, run_id=100, abstract_id=None, venue=venue,
                    scope_version='before', relevance_class='core' if include else 'out_of_scope',
                    relevance_score='90', include_in_survey=include,
                    reason_codes='[]', rationale='Synthetic measured hardware',
                    screening_source='pdf_review', review_status='reviewed', screened_at='before')

    def evidence(self, pid, field, mid=None):
        source = self.qualified['rows'][pid - 1]
        result = dict(paper_id=pid, field_name=field, source_kind='pdf',
                      source_sha256=source['pdf_sha256'], source_locator='PDF p.1',
                      evidence_text='Measured prototype result with explicit hardware scope.',
                      source_url=f'/pdf/{source["article"]}#page=1', confidence='0.9',
                      review_status='reviewed')
        if mid is not None:
            result['measurement_id'] = mid
        return result

    def validate(self):
        apply.validate_plan(self.plan, self.original, self.qualified)

    def test_valid_small_ieee_optica_plan_and_eligibility_only_hold_pass(self):
        self.validate()

    def test_population_must_equal_qualified_and_have_no_duplicate_targets(self):
        original = deepcopy(self.plan['targets'])
        for targets in (original[:-1], original + [deepcopy(original[0])]):
            with self.subTest(size=len(targets)), self.assertRaises(ValueError):
                self.plan['targets'] = targets
                self.validate()

    def test_nonqualified_verdict_and_source_fail(self):
        for field, value in (('verdict', '참고 후보'), ('source_system', 'nature')):
            old = self.qualified['rows'][0][field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.qualified['rows'][0][field] = value
                self.validate()
            self.qualified['rows'][0][field] = old

    def test_identity_and_snapshot_fingerprints_are_checked(self):
        self.original['papers'][0]['article_number'] = 'other'
        with self.assertRaises(ValueError):
            self.validate()
        self.original['papers'][0]['article_number'] = '1001'
        self.original['tables']['serdes_measurements'][0]['process_nm'] = '40'
        with self.assertRaises(ValueError):
            self.validate()

    def test_existing_value_cannot_be_overwritten_or_cleared(self):
        for fields in ({'lane_rate_gbps': '64'}, {'lane_rate_gbps': None}, {'process_nm': None}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.plan['targets'][0]['updates'][0]['fields'] = fields
                self.validate()

    def test_protected_articles_allow_no_update_or_new_point(self):
        for aid in apply.PROTECTED_ARTICLES:
            self.original['papers'][2]['article_number'] = aid
            self.qualified['rows'][2]['article'] = aid
            self.plan['targets'][2]['updates'] = [dict(measurement_id=31, fields={'power_mw': '10'})]
            with self.subTest(article=aid, action='update'), self.assertRaises(ValueError):
                self.validate()
            self.plan['targets'][2]['updates'] = []
            self.plan['targets'][2]['new_points'] = deepcopy(self.plan['targets'][1]['new_points'])
            with self.subTest(article=aid, action='new_point'), self.assertRaises(ValueError):
                self.validate()
            self.plan['targets'][2]['new_points'] = []

    def test_numeric_new_points_require_finite_positive_values_and_integer_lanes(self):
        point = self.plan['targets'][1]['new_points'][0]
        for field, value in (('lane_rate_gbps', '-56'), ('lane_rate_gbps', 'NaN'),
                             ('lane_rate_gbps', 'Infinity'), ('lane_count', '1.5'),
                             ('ber', '1.1'), ('energy_pj_bit', '0')):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                trial = deepcopy(point)
                trial['values'][field] = value
                trial['evidence'].append(self.evidence(2, field))
                self.plan['targets'][1]['new_points'] = [trial]
                self.validate()
        self.plan['targets'][1]['new_points'] = [point]

    def test_numeric_null_fills_have_same_unit_checks_as_new_points(self):
        for field, value in (('process_nm', '-28'), ('power_mw', '-1'),
                             ('lane_count', '1.5'), ('ber', '1.1')):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.plan['targets'][0]['updates'][0]['fields'] = {field: value}
                self.plan['targets'][0]['evidence'] = [self.evidence(1, field, 11)]
                self.validate()

    def test_every_inserted_and_updated_field_requires_associated_pdf_evidence(self):
        self.plan['targets'][0]['evidence'] = []
        with self.assertRaises(ValueError):
            self.validate()
        self.plan['targets'][0]['evidence'] = [self.evidence(1, 'process_nm', 11)]
        self.plan['targets'][1]['new_points'][0]['evidence'] = []
        with self.assertRaises(ValueError):
            self.validate()

    def test_evidence_rejects_wrong_pdf_hash_empty_quote_or_other_paper(self):
        original = self.plan['targets'][0]['evidence'][0]
        for field, value in (('source_sha256', '0' * 64), ('evidence_text', '  '), ('paper_id', 2)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.plan['targets'][0]['evidence'] = [{**original, field: value}]
                self.validate()

    def test_evidenced_sources_must_be_covered_by_apply_time_file_hash_checks(self):
        self.plan['sources'] = self.plan['sources'][1:]
        with self.assertRaises(ValueError):
            self.validate()

    def test_existing_evidence_and_new_point_must_link_to_target_implementation(self):
        self.plan['targets'][0]['evidence'][0]['measurement_id'] = 31
        with self.assertRaises(ValueError):
            self.validate()
        self.plan['targets'][0]['evidence'][0]['measurement_id'] = 11
        self.plan['targets'][1]['implementation_id'] = 30
        with self.assertRaises(ValueError):
            self.validate()

    def test_new_classification_cannot_write_other_table_or_other_paper(self):
        for table, value in (('serdes_measurements', {'paper_id': 1}),
                             ('serdes_paper_link_media', {'paper_id': 4, 'link_medium': 'optical'})):
            with self.subTest(table=table), self.assertRaises(ValueError):
                self.plan['targets'][0]['new_classifications'] = {table: value}
                self.validate()

    def post_state(self):
        after = deepcopy(self.original['tables'])
        after['serdes_measurements'][0]['process_nm'] = '28'
        after['serdes_measurements'][0]['updated_at'] = 'after'
        after['serdes_measurements'].append({**self.plan['targets'][1]['new_points'][0]['values'],
                                            'id': 21, 'implementation_id': 20,
                                            'overall_confidence': Decimal('0.900')})
        after['serdes_implementations'].append({**self.plan['targets'][1]['implementation'], 'id': 20})
        after['serdes_implementation_papers'].append(dict(implementation_id=20, paper_id=2,
                                                         relation_type='primary'))
        after['serdes_measurement_metric_scopes'].append(dict(measurement_id=21,
                                                            energy_component_scope='unknown'))
        after['serdes_paper_screenings'].append(self.screen(2, venue='OFC'))
        for row in after['serdes_paper_screenings']:
            row['run_id'] = 101 if row['venue'] == 'JSSC' else 102
        after['serdes_screening_runs'].extend([
            dict(id=101, venue='JSSC', status='complete', scope_name='all_measured_review'),
            dict(id=102, venue='OFC', status='complete', scope_name='all_measured_review')])
        receipt = dict(carried_paper_ids=[4], new_measurement_ids=[21],
                       new_implementation_ids=[20], run_ids=[101, 102])
        return after, receipt

    def test_postcheck_accepts_exact_sparse_fill_and_non_target_run_carry(self):
        after, receipt = self.post_state()
        apply.assert_post_state(self.original['tables'], after, self.plan, receipt)

    def test_postcheck_rejects_modified_existing_value_hold_family_or_non_target(self):
        for kind in ('numeric', 'hold', 'family', 'non_target', 'energy_scope', 'deleted_measurement'):
            after, receipt = self.post_state()
            if kind == 'numeric': after['serdes_measurements'][0]['lane_rate_gbps'] = '64'
            elif kind == 'hold': after['serdes_measurements'][1]['energy_pj_bit'] = '1.1'
            elif kind == 'family': after['serdes_implementation_families'][0]['label'] = 'changed'
            elif kind == 'non_target': after['serdes_paper_screenings'][2]['include_in_survey'] = 1
            elif kind == 'energy_scope': after['serdes_measurement_metric_scopes'][0]['energy_component_scope'] = 'trx'
            elif kind == 'deleted_measurement': after['serdes_measurements'].pop(0)
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                apply.assert_post_state(self.original['tables'], after, self.plan, receipt)

    def test_postcheck_rejects_carry_that_loses_current_population(self):
        after, receipt = self.post_state()
        after['serdes_paper_screenings'][2]['run_id'] = 100
        with self.assertRaises(ValueError):
            apply.assert_post_state(self.original['tables'], after, self.plan, receipt)

    def test_postcheck_rejects_new_point_value_different_from_the_approved_plan(self):
        after, receipt = self.post_state()
        after['serdes_measurements'][-1]['lane_rate_gbps'] = '64'
        with self.assertRaises(ValueError):
            apply.assert_post_state(self.original['tables'], after, self.plan, receipt)

    def test_postcheck_allows_same_operating_key_in_distinct_implementations(self):
        point = deepcopy(self.plan['targets'][1]['new_points'][0])
        point['evidence'] = [self.evidence(1, 'lane_rate_gbps'), self.evidence(1, 'lane_count')]
        self.plan['targets'][0]['new_points'] = [point]
        after, receipt = self.post_state()
        after['serdes_measurements'].append({**point['values'], 'id': 12,
                                            'implementation_id': 10, 'overall_confidence': Decimal('0.900')})
        after['serdes_measurement_metric_scopes'].append(dict(measurement_id=12,
                                                            energy_component_scope='unknown'))
        receipt['new_measurement_ids'].append(12)
        apply.assert_post_state(self.original['tables'], after, self.plan, receipt)

    def test_postcheck_matches_positive_mysql_decimal_rounding(self):
        self.plan['targets'][1]['new_points'][0]['values']['lane_rate_gbps'] = '56.1234565'
        after, receipt = self.post_state()
        after['serdes_measurements'][-1]['lane_rate_gbps'] = Decimal('56.123457')
        apply.assert_post_state(self.original['tables'], after, self.plan, receipt)

    def test_read_only_preflight_rolls_back_and_never_mutates(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        with patch.object(apply, 'read_tables', return_value=self.original['tables']):
            result = apply.execute(conn, self.plan, self.original)
        self.assertFalse(result['applied'])
        conn.commit.assert_not_called()
        conn.rollback.assert_called_once()
        statements = [call.args[0] for call in cur.execute.call_args_list]
        self.assertEqual(statements, ['SET TRANSACTION READ ONLY', 'START TRANSACTION WITH CONSISTENT SNAPSHOT'])

    def test_transaction_does_not_commit_before_postcheck(self):
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = {'acquired': 1}
        with patch.object(apply, 'read_tables', return_value=self.original['tables']), \
             patch.object(apply, 'insert_row', return_value=999), \
             patch.object(apply, 'store_evidence', return_value=1), \
             patch.object(apply, 'assert_post_state', side_effect=ValueError('readback mismatch')):
            with self.assertRaisesRegex(ValueError, 'readback mismatch'):
                apply.execute(conn, self.plan, self.original, apply=True)
        conn.commit.assert_not_called()

    def test_main_rolls_back_and_closes_on_precommit_failure(self):
        self.plan['snapshot_path'] = str(self.directory / 'snapshot.json')
        self.plan['qualified_path'] = str(self.directory / 'qualified.json')
        for key, value in (('snapshot', self.original), ('qualified', self.qualified)):
            path = Path(self.plan[f'{key}_path'])
            path.write_text(json.dumps(value), encoding='utf8')
            self.plan[f'{key}_sha256'] = apply.digest(path.read_bytes())
        plan_path, report = self.directory / 'plan.json', self.directory / 'receipt.json'
        plan_path.write_text(json.dumps(self.plan), encoding='utf8')
        conn = MagicMock()
        with patch('sys.argv', ['apply', '--plan', str(plan_path), '--report', str(report), '--apply']), \
             patch.object(apply, 'connect', return_value=conn), \
             patch.object(apply, 'execute', side_effect=ValueError('postcheck failed')):
            with self.assertRaisesRegex(ValueError, 'postcheck failed'):
                apply.main()
        conn.rollback.assert_called_once()
        conn.close.assert_called_once()
        conn.commit.assert_not_called()
        self.assertFalse(report.exists())


if __name__ == '__main__':
    unittest.main()
