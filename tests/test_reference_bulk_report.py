from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import report_reference_bulk as reporter
from scripts.benchmark_reference_bulk import save


class BulkReportTests(unittest.TestCase):
    def setup_output(self, out):
        save(out / 'manifest.json', {'papers': [{'id': 'a'}]})
        save(out / 'llm-settings.json', {'signature': {'model': 'test'}})
        save(out / 'prepare-status.json', {'status': 'running', 'pid': 1})
        save(out / 'enrich-status.json', {'status': 'running', 'pid': 2})

    def test_stale_signature_not_counted_as_processed(self):
        with tempfile.TemporaryDirectory() as d, patch.object(reporter, 'alive', return_value=True):
            out = Path(d)
            self.setup_output(out)
            save(out / 'papers/a/result.json', {'signature': {'model': 'old'}})
            result = reporter.progress(out)
            self.assertEqual(result['processed'], 0)
            self.assertEqual(result['status'], 'running')

    def test_dead_worker_reported_without_claiming_completion(self):
        with tempfile.TemporaryDirectory() as d, patch.object(reporter, 'alive', return_value=False):
            out = Path(d)
            self.setup_output(out)
            self.assertEqual(reporter.progress(out)['status'], 'needs_attention')

    def test_access_denied_is_unknown_not_dead(self):
        with tempfile.TemporaryDirectory() as d, patch.object(reporter, 'alive', return_value=None):
            out = Path(d)
            self.setup_output(out)
            self.assertEqual(reporter.progress(out)['status'], 'running')

    def test_missing_reference_does_not_score_null_fields_as_correct(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            save(out / 'manifest.json', {'papers': [{'id': 'a', 'pdf': 'a.pdf'}]})
            save(out / 'holdout-audit-gold.json', {'scope': 'test', 'papers': [{'pdf': 'a.pdf',
                'expected_count': 2, 'gold': [
                    {'number': 1, 'title': None, 'author': 'Lee', 'year': 2020, 'doi': None},
                    {'number': 2, 'title': None, 'author': None, 'year': None, 'doi': None}]}]})
            save(out / 'papers/a/result.json', {'rows': [
                {'number': 1, 'title': None, 'author': 'Lee', 'year': 2020, 'doi': None}]})
            score = reporter.holdout_score(out)
            self.assertEqual(score['pipelines']['rows']['correct_fields'], 4)
            self.assertEqual(score['pipelines']['rows']['checked_fields'], 8)
            self.assertEqual(score['count_exact_papers'], 0)

    def test_final_pipeline_scores_display_title_without_changing_prior_stages(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            save(out / 'manifest.json', {'papers': [{'id': 'a', 'pdf': 'a.pdf'}]})
            save(out / 'holdout-audit-gold.json', {'scope': 'test', 'papers': [{'pdf': 'a.pdf',
                'expected_count': 1, 'gold': [{'number': 1, 'title': 'A driver'}]}]})
            original = {'number': 1, 'title': 'A driver[J]'}
            save(out / 'papers/a/result.json', {'rows': [{**original, 'title_display': 'A driver'}],
                'grobid': [original], 'before_llm': [original]})
            score = reporter.holdout_score(out)['pipelines']
            self.assertEqual(score['rows']['correct_fields'], 1)
            self.assertEqual(score['grobid']['correct_fields'], 0)
            self.assertEqual(score['before_llm']['correct_fields'], 0)


if __name__ == '__main__':
    unittest.main()
