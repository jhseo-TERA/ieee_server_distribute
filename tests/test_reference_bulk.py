"""Operational guardrails for the held-out, resumable PDF experiment."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import benchmark_reference_bulk as bulk


class BulkReferenceTests(unittest.TestCase):
    def test_allocation_preserves_requested_count_and_small_publishers(self):
        self.assertEqual(bulk.allocate({'a': 1690, 'b': 211, 'c': 17}, 500),
                         {'a': 441, 'b': 55, 'c': 4})
        with self.assertRaises(ValueError):
            bulk.allocate({'a': 2}, 3)

    def test_no_heading_does_not_silently_crop_reference_pages(self):
        pages = [(i, 'text') for i in range(1, 101)]
        scan = bulk.scan_pages(pages)
        self.assertEqual((scan['start'], scan['end']), (1, 100))
        self.assertFalse(scan['heading_found'])

    def test_heading_keeps_reference_continuations_on_later_pages(self):
        scan = bulk.scan_pages([(1, 'body'), (2, 'VI. REFERENCES\n[1] A'), (3, '[2] B')])
        self.assertEqual((scan['start'], scan['end']), (2, 3))

    def test_batches_preserve_items_and_bound_source_characters(self):
        entries = [{'number': n, 'raw': 'x' * 2000} for n in range(1, 10)]
        grouped = list(bulk.batches(entries))
        self.assertEqual([len(g) for g in grouped], [3, 3, 3])
        self.assertEqual([e['number'] for g in grouped for e in g], list(range(1, 10)))
        self.assertEqual([len(g) for g in bulk.batches([{'raw': 'x'}] * 12)], [5, 5, 2])

    def test_failed_model_batch_splits_and_reuses_exact_cache(self):
        class Guard:
            def wait(self):
                pass
        entries = [{'number': n, 'raw': 'text'} for n in (1, 2)]
        calls = []

        def model(client, name, rows):
            calls.append([e['number'] for e in rows])
            if len(rows) > 1:
                raise ValueError('Output incomplete')
            return rows, {'elapsed_seconds': 1}

        with tempfile.TemporaryDirectory() as directory, patch.object(bulk, 'llm_fields', model):
            rows, times, errors = bulk.llm_batch(None, Guard(), entries, Path(directory), {'model': 'test'})
            self.assertEqual([r['number'] for r in rows], [1, 2])
            self.assertTrue(errors[0]['recovered_by_split'])
            self.assertEqual(len(times), 2)
            bulk.llm_batch(None, Guard(), entries, Path(directory), {'model': 'test'})
            self.assertEqual(calls, [[1, 2], [1], [2]])

    def test_grobid_group_maps_repeated_labels_to_their_source_pdf(self):
        papers = [{'id': name, 'pdf': name + '.pdf'} for name in ('a', 'b')]
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            for paper in papers:
                entry = {'number': 1, 'page': 2, 'raw': 'A. Lee, "' + paper['id'] + ' title," 2020.'}
                bulk.save(out / 'papers' / paper['id'] / 'prepared.json', {'entries': [entry],
                    'status': 'ready', 'entry_source': 'mineru', 'warnings': [], 'errors': []})

            def grobid(rows, target):
                return [bulk.grounded(e, {'title': e['raw'].split('"')[1].rstrip(','),
                    'author': 'Lee', 'year': 2020}, 'grobid') for e in rows], 1

            with patch.object(bulk, 'grobid_raw', grobid):
                bulk.enrich_group(papers, out, None, None, {'model': 'test'})
            for name in ('a', 'b'):
                result = bulk.read(out / 'papers' / name / 'result.json')
                self.assertEqual(result['rows'][0]['title'], name + ' title')
                self.assertEqual(result['routed'], 0)

    def test_adapter_change_reuses_only_identical_model_settings(self):
        import hashlib
        import json
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            old = {'model': 'same', 'model_digest': 'abc', 'temperature': .1, 'grobid_adapter': 'old'}
            bulk.save(out / 'llm/old.json', {'signature': old, 'input': '[]', 'rows': [], 'timings': []})
            desired = {**old, 'grobid_adapter': 'new'}
            bulk.migrate_llm_caches(out, desired)
            reduced = {k: v for k, v in desired.items() if k != 'grobid_adapter'}
            sha = hashlib.sha256(('[]' + json.dumps(reduced, sort_keys=True)).encode()).hexdigest()
            self.assertEqual(bulk.read(out / 'llm' / (sha + '.json'))['reused_from'], 'old.json')

    def test_changed_model_digest_cannot_reuse_old_answers(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            bulk.save(out / 'llm/old.json', {'signature': {'model_digest': 'old', 'grobid_adapter': 'old'},
                'input': '[]', 'rows': [], 'timings': []})
            bulk.migrate_llm_caches(out, {'model_digest': 'different', 'grobid_adapter': 'new'})
            self.assertEqual(len(list((out / 'llm').glob('*.json'))), 1)

    def test_model_cache_uses_only_actual_prompt_fields_and_generation_settings(self):
        settings={'model_digest':'same','prompt_sha256':'same','temperature':.1,'references_code':'old','role_code':'old'}
        changed={**settings,'references_code':'new','role_code':'new'}
        self.assertEqual(bulk.llm_cache_signature(settings),bulk.llm_cache_signature(changed))
        self.assertNotEqual(bulk.llm_cache_signature(settings),bulk.llm_cache_signature({**changed,'prompt_sha256':'new'}))
        a=[{'number':1,'raw':'same source','page':1,'warnings':['old']}]
        b=[{'number':1,'raw':'same source','page':2,'warnings':[]}]
        self.assertEqual(bulk.llm_input(a),bulk.llm_input(b))
        self.assertNotEqual(bulk.llm_input(a),bulk.llm_input([{**b[0],'raw':'different source'}]))

    def test_partial_model_cache_cannot_shift_answer_to_a_different_reference(self):
        class Guard:
            def wait(self):pass
        entries=[{'number':n,'page':1,'raw':f'A. Lee, "Title {n}," 2020.'} for n in (1,2)]
        with tempfile.TemporaryDirectory() as directory, patch.object(bulk,'llm_fields',
                return_value=([{**entries[1],'title':'Title 2'}],{'elapsed_seconds':1})) as model:
            bulk.llm_batch(None,Guard(),entries,Path(directory),{'model':'same'})
            moved=[{**entry,'page':9} for entry in entries]
            rows,_,_=bulk.llm_batch(None,Guard(),moved,Path(directory),{'model':'same'})
            self.assertEqual([(row['number'],row['page']) for row in rows],[(2,9)])
            self.assertEqual(model.call_count,1)

    def test_completed_experiments_cannot_be_overwritten_by_batch_cli(self):
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder)
            bulk.require_mutable_output(out)
            (out/'replay-summary.json').write_text('{}',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'immutable'):
                bulk.require_mutable_output(out)


if __name__ == '__main__':
    unittest.main()
