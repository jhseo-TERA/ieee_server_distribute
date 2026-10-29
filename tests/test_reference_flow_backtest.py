"""Backtest scoring counts missing rows as failures, including expected null fields."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import backtest_reference_flow as backtest


class FlowBacktestScoringTests(unittest.TestCase):
    def score(self, expected, rows, previous):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            out, validation, prior = root / 'out', root / 'validation', root / 'prior'
            paper = {'id': 'one', 'audit_id': 'p001', 'cohort': 'fixture', 'pdf': 'one.pdf'}
            save = backtest.bulk.save
            save(out / 'manifest.json', {'papers': [paper]})
            save(validation / 'gold/p001.json', expected)
            save(prior / 'papers/one/result.json', {'rows': previous})
            save(out / 'papers/one/result.json', {'rows': rows, 'status': 'complete',
                'routed': 0, 'llm_completed': 0, 'entry_source': 'native_text'})
            save(out / 'papers/one/prepared.json', {'mineru_skipped': True,
                'ocr': {'status': 'not_needed'}, 'elapsed_seconds': 1})
            (out / 'runtime-events.jsonl').write_text('', encoding='utf-8')
            with patch.object(backtest, 'COHORTS', {'fixture': (validation, prior)}), \
                    patch.object(backtest, 'verify'), redirect_stdout(StringIO()):
                return backtest.report(out)

    def test_missing_row_is_four_errors_even_when_expected_fields_are_all_null(self):
        reference = {'number': 1, 'title': None, 'author': None, 'year': None, 'doi': None}
        result = self.score({'expected_count': 1, 'references': [reference]}, [], [reference])
        self.assertEqual((result['fields_checked'], result['field_errors']), (4, 4))
        self.assertEqual(result['field_error_rate_percent'], 100)
        self.assertEqual(result['count_exact_papers'], 0)
        self.assertEqual(result['regressions_vs_previous_accepted_rows'], 4)
        self.assertTrue(all(case['row_missing'] for case in result['mismatches']))

    def test_unscored_fields_and_regressions_use_actual_sample_denominators(self):
        first = {'number': 1, 'title': 'First', 'author': 'Lee', 'year': 2020, 'doi': None}
        second = {'number': 2, 'title': 'Second', 'year': 2021}
        result = self.score({'expected_count': 2, 'references': [first, second]},
            [{**first, 'author': 'Wrong', 'needs_review': True}, second],
            [first, {**second, 'title': 'Incorrect'}])
        self.assertEqual((result['fields_checked'], result['field_errors']), (6, 1))
        self.assertEqual(result['field_error_rate_percent'], 16.667)
        self.assertEqual(result['sampled_references'], 2)
        self.assertEqual(result['fields']['author']['checked'], 1)
        self.assertEqual(result['count_exact_papers'], 1)
        self.assertEqual(result['regressions_vs_previous_accepted_rows'], 1)
        self.assertEqual(result['improvements_vs_previous_accepted_rows'], 1)
        self.assertTrue(result['mismatches'][0]['review_marked'])


if __name__ == '__main__':
    unittest.main()
