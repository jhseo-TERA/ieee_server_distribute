import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_ai_repository import LocalAIRepository, approval_groups, validate_proposal


def candidate(**updates):
    value = {
        'field': 'lane_rate_gbps', 'value': 112, 'unit': 'Gbps',
        'page': 2, 'quote': 'The receiver operates at 112 Gb/s per lane.',
        'source_sha256': 'a' * 64, 'operating_point': '112G nominal measured',
        'rate_scope': 'lane', 'power_scope': 'unknown', 'component_scope': 'rx',
        'validation_status': 'valid',
    }
    value.update(updates)
    return value


def stored(pid=1, **updates):
    item = validate_proposal(candidate(**updates))
    return {
        'id': pid, 'job_id': '95643ab7-ed24-40d1-9f9b-2ebd4eb53324', 'paper_id': 3,
        'article_number': '123456', 'title': '112 Gb/s receiver', 'year': '2025',
        'source_name': 'ISSCC', 'source_system': 'ieee', 'pdf_available': 1,
        'pdf_local_path': 'ieee-pdf/123456.pdf', 'model': 'gpt-oss:20b',
        'field_name': item['field'], 'proposed_value': json.dumps(item['value']),
        'unit': item['unit'], 'source_page': item['page'], 'evidence_quote': item['quote'],
        'source_sha256': item['source_sha256'], 'operating_point': item['operating_point'],
        'validation_status': item['validation_status'], 'payload_json': json.dumps(item),
        'status': 'pending', 'job_status': 'complete',
    }


class FakeResult:
    def __init__(self, rows=None, lastrowid=1, rowcount=1):
        self.rows = rows or []
        self.lastrowid = lastrowid
        self.rowcount = rowcount

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class FakeConnection:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement, parameters=None):
        sql = str(statement)
        self.calls.append((sql, parameters))
        return FakeResult(self.rows if sql.lstrip().startswith('SELECT') else [], lastrowid=len(self.calls))


class FakeEngine:
    def __init__(self, rows=None):
        self.conn = FakeConnection(rows)

    def begin(self):
        return self.conn

    def connect(self):
        return self.conn


class SearchTests(unittest.TestCase):
    def test_query_is_parameterized_with_literal_wildcards(self):
        sql, params, _, _ = LocalAIRepository.build_search("112% _x';drop", 'repo')
        self.assertNotIn("_x';drop", sql)
        self.assertEqual(params['q0'], '%112!%%')
        self.assertEqual(params['q1'], "%!_x';drop%")

    def test_all_numeric_conditions_share_same_measurement(self):
        sql, params, metric, scope = LocalAIRepository.build_search(filters={
            'min_rate_gbps': 112, 'max_energy_pj_bit': 2, 'process_nm_max': 28,
        })
        self.assertEqual(sql.count('EXISTS (SELECT 1 FROM serdes_measurements'), 1)
        self.assertIn('m.energy_pj_bit <= :max_energy_pj_bit', metric)
        self.assertIn('m.process_nm <= :process_nm_max', metric)
        self.assertIn("m.rate_scope='lane'", metric)
        self.assertNotIn('aggregate_rate', metric)
        self.assertEqual(scope, 'lane')
        self.assertIn("scope_name LIKE 'all%'", sql)
        self.assertIn('s.include_in_survey=1', sql)

    def test_explicit_aggregate_rate(self):
        _, _, metric, _ = LocalAIRepository.build_search(filters={'min_rate_gbps': 400, 'rate_scope': 'aggregate'})
        self.assertIn('m.aggregate_rate_gbps', metric)
        self.assertNotIn('m.lane_rate_gbps', metric)

    def test_unknown_identifiers_rejected(self):
        for filters in ({'sort': 'DROP TABLE papers'}, {'made_up': 2},
                        {'min_rate_gbps': float('nan')}, {'rate_scope': 'unknown'},
                        {'year_from': 2025, 'year_to': 2020}):
            with self.subTest(filters=filters), self.assertRaises(ValueError):
                LocalAIRepository.build_search(filters=filters)

    def test_order_preserved_for_paper_selection(self):
        repo = LocalAIRepository(FakeEngine([{'article_number': '2'}, {'article_number': '1'}]))
        self.assertEqual([x['article_number'] for x in repo.get_papers(['1', '2', '1'])], ['1', '2'])

    def test_numeric_limits(self):
        _, params, _, _ = LocalAIRepository.build_search(limit=999)
        self.assertEqual(params['limit'], 50)
        with self.assertRaises(ValueError):
            LocalAIRepository.build_search(query='x' * 501)


class ProposalTests(unittest.TestCase):
    def test_canonical_units_and_scope(self):
        self.assertEqual(validate_proposal(candidate(unit='Gb/s'))['unit'], 'Gbps')
        for changes in ({'value': -1}, {'value': True}, {'unit': 'Mbps'},
                        {'rate_scope': 'unknown'}, {'page': 1.5}, {'field': 'title'},
                        {'source_sha256': 'wrong'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_proposal(candidate(**changes))

    def test_valid_group_and_conflicts(self):
        groups = approval_groups([stored(), stored(2, field='energy_pj_bit', value=2, unit='pJ/bit')])
        self.assertEqual(groups[0]['values'], {'lane_rate_gbps': 112.0, 'energy_pj_bit': 2.0})
        self.assertEqual(groups[0]['scopes']['component_scope'], 'rx')
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            approval_groups([stored(), stored(2, value=224)])

    def test_unknown_operating_points_are_isolated(self):
        groups = approval_groups([stored(1, operating_point='unknown'), stored(2, operating_point='unknown')])
        self.assertEqual(len(groups), 2)

    def test_scope_unknown_does_not_inherit_from_other_field(self):
        with self.assertRaisesRegex(ValueError, 'component'):
            approval_groups([stored(), stored(2, field='energy_pj_bit', value=2,
                unit='pJ/bit', component_scope='unknown')])

    def test_pdf_grounding_gate(self):
        for key, value in [('validation_status', 'needs_review'), ('evidence_quote', ''),
                           ('source_page', None), ('source_sha256', ''),
                           ('source_system', 'nature'), ('pdf_available', 0), ('status', 'approved'),
                           ('job_status', 'cancelled'), ('job_status', 'failed'), ('job_status', 'running')]:
            row = stored()
            row[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                approval_groups([row])

    def test_visual_requires_explicit_true(self):
        row = stored(validation_status='visual_review')
        with self.assertRaises(ValueError):
            approval_groups([row])
        with self.assertRaises(ValueError):
            approval_groups([row], confirm_visual='true')
        self.assertEqual(len(approval_groups([row], confirm_visual=True)), 1)

    def test_mixed_pdf_revisions_blocked(self):
        with self.assertRaisesRegex(ValueError, 'different PDF'):
            approval_groups([stored(), stored(2, source_sha256='b' * 64)])

    def test_rejected_proposals_do_not_write_survey(self):
        engine = FakeEngine([stored()])
        repo = LocalAIRepository(engine)
        result = repo.decide_proposals([1], 'reject', 'reviewer')
        self.assertEqual(result['measurement_ids'], [])
        self.assertFalse(any('INSERT INTO serdes' in sql for sql, _ in engine.conn.calls))
        self.assertIn('FOR UPDATE', engine.conn.calls[0][0])

    def test_approval_is_additive_with_per_field_evidence(self):
        engine = FakeEngine([stored()])
        repo = LocalAIRepository(engine)
        with patch.object(repo, '_verify_pdf_revision') as verify:
            result = repo.decide_proposals([1], 'approve', 'human-reviewer')
        verify.assert_called_once()
        self.assertEqual(len(result['measurement_ids']), 1)
        sql = '\n'.join(sql for sql, _ in engine.conn.calls)
        self.assertNotIn('DELETE', sql.upper())
        self.assertNotIn('UPDATE serdes_', sql)
        evidence = next(params for sql, params in engine.conn.calls if 'INSERT INTO serdes_measurement_evidence' in sql)
        self.assertEqual(evidence['sha'], 'a' * 64)
        self.assertIn('model gpt-oss:20b', evidence['locator'])

    def test_invalid_approval_writes_nothing(self):
        row = stored(validation_status='needs_review')
        engine = FakeEngine([row])
        with self.assertRaises(ValueError):
            LocalAIRepository(engine).decide_proposals([1], 'approve', 'reviewer')
        self.assertEqual(len(engine.conn.calls), 1)

    def test_pdf_revision_and_root_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'ieee-pdf').mkdir()
            data = b'%PDF-1.4 test'
            (root / 'ieee-pdf/test.pdf').write_bytes(data)
            repo = LocalAIRepository(None, root=root)
            row = {'pdf_local_path': 'ieee-pdf/test.pdf', 'source_sha256': hashlib.sha256(data).hexdigest()}
            repo._verify_pdf_revision(row)
            with self.assertRaisesRegex(ValueError, 'changed'):
                repo._verify_pdf_revision({**row, 'source_sha256': 'a' * 64})
            with self.assertRaisesRegex(ValueError, 'outside'):
                repo._verify_pdf_revision({**row, 'pdf_local_path': '../outside.pdf'})


class JobTests(unittest.TestCase):
    def test_owner_filter_and_json_normalization(self):
        engine = FakeEngine([{'id': 'x', 'request_json': '{"query":"PAM4"}', 'result_json': None}])
        job = LocalAIRepository(engine).get_job('x', owner='alice')
        self.assertEqual(job['request']['query'], 'PAM4')
        self.assertIsNone(job['result'])
        self.assertIn('AND owner=:owner', engine.conn.calls[0][0])

    def test_job_claim_is_atomic_and_fields_immutable(self):
        engine = FakeEngine()
        repo = LocalAIRepository(engine)
        self.assertTrue(repo.claim_job('x'))
        self.assertIn("AND status='queued'", engine.conn.calls[0][0])
        with self.assertRaises(ValueError):
            repo.update_job('x', owner='attacker')
        with self.assertRaises(ValueError):
            repo.update_job('x', status='deleted')

    def test_terminal_worker_update_cannot_revive_cancelled_job(self):
        engine = FakeEngine()
        repo = LocalAIRepository(engine)
        for status in ('complete', 'failed'):
            repo.update_job('x', status=status, result={'answer': 'late output'})
            self.assertIn("WHERE id=:id AND status='running'", engine.conn.calls[-1][0])
        repo.update_job('x', status='cancelled')
        self.assertNotIn("AND status='running'", engine.conn.calls[-1][0])


if __name__ == '__main__':
    unittest.main()
