"""A URL-only reference must not erase the surrounding bibliography or use a model."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_server.parsers.reference_pipeline import reconcile_rows
from paper_server.parsers.reference_segmentation import segment_references
from paper_server.parsers.reference_urls import standalone_url
from paper_server.parsers.references import FIELDS, consensus, grobid_raw
from scripts import benchmark_reference_bulk as bulk


class URLReferenceTests(unittest.TestCase):
    def test_middle_url_keeps_numbering_and_later_references_in_both_styles(self):
        for label in (lambda n: f'[{n}]', lambda n: f'{n}.'):
            text = ('References\n' + label(1) + ' A. Lee, "First title," 2020.\n'
                    + label(2) + ' https://rfic.ece.ucsb.edu/files/opticalrxsurveyv0xlsx\n'
                    + label(3) + ' B. Wu, "Last title," 2021.')
            result = segment_references([(3, text)])
            self.assertEqual(result['status'], 'ready')
            self.assertEqual([r['number'] for r in result['entries']], [1, 2, 3])
            self.assertEqual(result['entries'][2]['page'], 3)
            self.assertIn('Last title', result['entries'][2]['raw'])

    def test_detector_does_not_turn_a_paper_or_prose_into_a_url_slot(self):
        for raw in ('A. Lee, "Real title," 2020. https://example.org/paper',
                    'Text with https://example.org/link', 'https://example.org/link trailing prose',
                    'http://', 'https://[broken', 'arbitrary text'):
            self.assertIsNone(standalone_url(raw))
        self.assertEqual(standalone_url('[Online]. Available: https://example.org/file.'),
                         'https://example.org/file')
        result = segment_references([(1, 'References\n[1] A. Lee, "Title," 2020.\n[2] arbitrary prose')])
        self.assertEqual(result['status'], 'extraction_unresolved')

    def test_cached_model_guesses_cannot_populate_url_metadata_or_trigger_review(self):
        entry = {'number': 4, 'page': 3, 'raw': 'https://example.org/2020/10.1234/report'}
        guessed = {**entry, 'title': entry['raw'], 'author': 'example', 'year': 2020,
                   'doi': '10.1234/report', 'journal': 'example', 'needs_review': True,
                   'needs_review_before_normalization': True, 'llm_status': 'failed',
                   'author_resolution': {'stale': True}}
        initial = consensus(entry, guessed, guessed, guessed)
        final = reconcile_rows([guessed])[0]
        for row in (initial, final):
            self.assertTrue(all(row[field] is None for field in FIELDS))
            self.assertEqual(row['processing_status'], 'logged_only')
            self.assertFalse(row['needs_llm'])
            self.assertFalse(row['needs_review'])
            self.assertEqual(row['llm_status'], 'not_needed')
            self.assertNotIn('author_resolution', row)
        self.assertEqual((final['number'], final['page'], final['raw']), (4, 3, entry['raw']))

    def test_url_only_batch_never_starts_grobid(self):
        entries = [{'number': n, 'page': 1, 'raw': f'https://example.org/{n}'} for n in (1, 2)]
        with patch('paper_server.parsers.references.subprocess.run') as run:
            rows, elapsed = grobid_raw(entries, 'unused')
        run.assert_not_called()
        self.assertEqual([r['number'] for r in rows], [1, 2])
        self.assertEqual(elapsed, 0)

    def test_bulk_continues_and_writes_source_bound_log_without_llm(self):
        paper = {'id': 'paper-a', 'pdf': 'paper-a.pdf', 'sha256': 'a' * 64}
        entries = segment_references([(2, 'References\n[1] A. Lee, "First title," 2020.\n'
            '[2] https://example.org/resource\n[3] B. Wu, "Last title," 2021.')])['entries']
        def grobid(rows, target):
            values = ({'title': 'First title', 'author': 'Lee', 'year': 2020},
                      {'title': 'https://example.org/resource', 'author': 'https'},
                      {'title': 'Last title', 'author': 'Wu', 'year': 2021})
            return [bulk.grounded(e, v, 'grobid') for e, v in zip(rows, values)], 0
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            directory = out / 'papers' / paper['id']
            bulk.save(directory / 'prepared.json', {'entries': entries, 'status': 'ready',
                'entry_source': 'pdf_text', 'warnings': [], 'errors': []})
            with patch.object(bulk, 'grobid_raw', grobid), patch.object(bulk, 'llm_batch') as llm:
                bulk.enrich_group([paper], out, None, None, {'model': 'test'})
            llm.assert_not_called()
            result = bulk.read(directory / 'result.json')
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(result['reference_counts'],
                {'total_entries': 3, 'bibliographic_entries': 2, 'url_only_logged': 1})
            self.assertEqual([r['title'] for r in result['rows']], ['First title', None, 'Last title'])
            log = json.loads((directory / result['url_reference_log']).read_text(encoding='utf-8'))
            self.assertEqual(log['source'], paper)
            self.assertEqual([(e['number'], e['page']) for e in log['events']], [(2, 2)])
            self.assertFalse(log['events'][0]['url_fetch_attempted'])


if __name__ == '__main__':
    unittest.main()
