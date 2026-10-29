"""Text wins; OCR is bounded, source-bound, cached, and explicitly reviewable."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from paper_server.parsers import reference_ocr as ocr
from paper_server.parsers.reference_evidence import bounded_evidence, authenticated_sources
from paper_server.parsers.reference_pipeline import reconcile_rows
from paper_server.parsers.reference_segmentation import choose_entries
from scripts import benchmark_reference_bulk as bulk


def native(pages):
    return {'source_sha256': 'a' * 64, 'extractor': 'pypdfium2',
            'pages': [{'page': n, 'text': text} for n, text in enumerate(pages, 1)]}


class ReferenceOCRTests(unittest.TestCase):
    def test_caption_controls_outside_readable_bibliography_do_not_force_ocr(self):
        data = native(['References\n[1] A. Lee, "A real title," 2020.\nFig. 1. Plot \x16 with labels.'])
        self.assertEqual(ocr.ocr_page_plan(data)['pages'], [])

    def test_ocr_footer_cannot_replace_a_clean_native_publication_year(self):
        raw = 'A. Lee, "A real title," IEEE JSSC, July 2017.'
        entry = {'number':1,'page':1,'raw':raw,'author':'Lee','year':2017,'title':'A real title'}
        data = native(['References\n[1] '+raw+'\n2018 EEF International Conference'])
        data['extractor'] = ocr.EXTRACTOR
        result = reconcile_rows([entry],bounded=bounded_evidence(data))[0]
        self.assertEqual(result['year'],2017)
        self.assertEqual(result['source_view']['raw'],raw)
        self.assertNotIn('ambiguous_publication_year',result.get('warnings',[]))
    def test_rare_control_in_long_reference_page_is_not_hidden_by_page_length(self):
        text = 'Readable text ' * 400 + '\nReferences\n[1] A. Lee, "A 0.13\x16m receiver," 2020.'
        plan = ocr.ocr_page_plan(native([text]))
        self.assertEqual(plan['pages'], [1])
        self.assertEqual(plan['reasons']['1'], 'damaged_native_text')

    def test_verified_discretionary_line_hyphen_alone_does_not_force_ocr(self):
        data = native(['References\n[1] A. Lee, "A high\ufffe\nspeed receiver," 2020.'])
        self.assertEqual(ocr.ocr_page_plan(data)['pages'], [])

    def test_ocr_repairs_damaged_entry_without_replacing_readable_neighbor(self):
        original = [
            {'number':1,'page':1,'raw':'A. Lee, "A 0.13\x16m receiver," 2020.'},
            {'number':2,'page':1,'raw':'B. Wu, "An accurate native title," 2021.'}]
        data = native(['References\n[1] A. Lee, "A 0.13um receiver," 2020.\n'
                       '[2] B. Wu, "An accurate natlve title," 2021.'])
        data['extractor'] = ocr.EXTRACTOR
        bounded = bounded_evidence(data)
        entries, diagnostic = choose_entries(original, bounded)
        self.assertIn('0.13um', entries[0]['raw'])
        self.assertEqual(entries[1]['raw'],original[1]['raw'])
        result = reconcile_rows(entries, bounded=bounded)
        self.assertEqual(result[1]['title_display'],'An accurate native title')
        self.assertEqual(result[1]['source_view']['extractor'],'original_reference')

    def test_ocr_lost_labels_cannot_erase_existing_native_bibliography(self):
        data = native(['References\n[1] A. Lee, "First," 2020.\nB. Wu, "Second," 2021.\n[3] C. Li, "Third," 2022.'])
        data['extractor'] = ocr.EXTRACTOR
        data['pages'][0]['native_text'] = ('References\n[1] A. Lee, "First," 2020.\n'
            '[2] B. Wu, "Second," 2021.\n[3] C. Li, "Third," 2022.')
        bounded = bounded_evidence(data)
        self.assertEqual(len(bounded['entries']),3)
        self.assertIn('ocr_bibliography_boundary_regression',bounded['warnings'])
        self.assertTrue(all(e['extractor']=='pypdfium2' for e in bounded['entries']))

    def test_bulk_incomplete_native_prefix_uses_layout_fallback(self):
        data = native(['References\n[1] A. Lee, "First," 2020.\nFig. 1. Body.\n'
                       '[2] B. Wu, "Second," 2021.'])
        data.update(evidence_path='evidence.json', evidence_sha256='b' * 64, ocr={'status':'not_needed'})
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            bulk.save(out / 'manifest.json', {'papers': [
                {'id': 'paper', 'pdf': 'local.pdf', 'sha256': 'a' * 64, 'bytes': 100}]})
            bulk.save(out / 'papers/paper/mineru/structured_content.json', {})
            entries = [{'number': n, 'page': 1, 'raw': f'A. Lee, "Title {n}," 2020.'} for n in (1,2)]
            with patch.object(bulk, 'digest_file', return_value='a'*64), \
                 patch.object(bulk, 'pdf_pages', return_value=[(1,data['pages'][0]['text'])]), \
                 patch.object(bulk, 'pdf_evidence', return_value=data), \
                 patch.object(bulk, 'mineru_references', return_value=entries), \
                 patch.object(bulk, 'MinerWorker') as miner:
                miner.return_value.run.return_value = {'seconds': 1}
                bulk.prepare(out)
            result = bulk.read(out/'papers/paper/prepared.json')
            self.assertEqual(len(result['entries']),2)
            self.assertEqual(result['entry_source'],'mineru')
            self.assertFalse(result['mineru_skipped'])
            self.assertNotIn('possible_omitted_references',result['warnings'])

    def test_readable_text_does_not_start_or_load_an_ocr_engine(self):
        data = native(['Readable native text ' * 30])
        with patch.object(ocr, 'ocr_settings') as settings, patch.object(ocr.subprocess, 'run') as run:
            result = ocr.with_ocr_fallback('unused.pdf', data, 'unused')
        settings.assert_not_called(); run.assert_not_called()
        self.assertEqual(result['ocr']['status'], 'not_needed')
        self.assertEqual(result['pages'], data['pages'])

    def test_mixed_document_only_ocr_reads_weak_reference_pages(self):
        data = native(['', 'References\n' + '[1] A. Lee, "A sufficiently long reference title," 2020. ' * 4, ''])
        plan = ocr.ocr_page_plan(data)
        self.assertEqual(plan['pages'], [3])
        self.assertTrue(plan['native_heading_found'])

    def test_short_but_valid_native_bibliography_does_not_need_ocr(self):
        data = native(['References\n[1] A. Lee, "A title," 2020.'])
        self.assertEqual(ocr.ocr_page_plan(data)['pages'], [])

    def test_replacement_glyphs_and_image_only_pages_are_candidates(self):
        data = native(['', 'A' * 300 + '\ufffd' * 30, 'Readable words ' * 30])
        self.assertEqual(ocr.ocr_page_plan(data)['pages'], [1, 2])

    def test_budget_does_not_silently_claim_the_whole_pdf_was_read(self):
        plan = ocr.ocr_page_plan(native([''] * 30))
        self.assertEqual(len(plan['pages']), 24)
        self.assertEqual(len(plan['candidate_pages']), 30)
        self.assertTrue(plan['budget_exceeded'])
        with self.assertRaises(ValueError):
            ocr.ocr_page_plan(native(['']), max_pages=25)

    def test_missing_runtime_keeps_original_text_and_reports_unresolved_ocr(self):
        data = native([''])
        with patch.object(ocr, 'ocr_settings', side_effect=FileNotFoundError):
            result = ocr.with_ocr_fallback('unused.pdf', data, 'unused')
        bounded = bounded_evidence(result)
        self.assertEqual(result['pages'], data['pages'])
        self.assertIn('ocr_unavailable', bounded['warnings'])
        self.assertEqual(bounded['status'], 'extraction_unresolved')

    def test_ocr_failure_does_not_discard_partial_native_references(self):
        data = native(['References\n[1] A. Lee, "A title," 2020.', ''])
        data['ocr'] = {'status': 'failed', 'pages': [2]}
        bounded = bounded_evidence(data)
        entries, diagnostics = choose_entries(bounded['entries'], bounded)
        self.assertEqual(len(entries), 1)
        self.assertEqual(diagnostics['source'], 'primary')
        self.assertIn('ocr_failed', diagnostics['warnings'])

    def test_same_page_and_model_reuse_cache_but_changed_model_reruns(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            data = native(['Readable words ' * 30, ''])
            path = folder / 'native.json'
            path.write_text(json.dumps(data), encoding='utf-8')
            data.update(evidence_path=str(path), evidence_sha256=ocr.file_hash(path))
            original = deepcopy(data)
            settings = {'version': ocr.VERSION, 'model_sha256': 'first'}
            def worker(command, **kwargs):
                request = json.loads(Path(command[3]).read_text(encoding='utf-8'))
                result = deepcopy(data)
                result['extractor'] = ocr.EXTRACTOR
                result['ocr'] = {'status': 'complete', 'settings': request['settings'], 'pages': [2]}
                result['pages'][1]['text'] = 'References\n[1] A. Lee, "Real title," 2020.'
                Path(command[5]).write_text(json.dumps(result), encoding='utf-8')
            with patch.object(ocr, 'ocr_settings', return_value=settings), patch.object(ocr.subprocess, 'run', side_effect=worker) as run:
                first = ocr.with_ocr_fallback('local.pdf', data, folder / 'cache')
                second = ocr.with_ocr_fallback('local.pdf', data, folder / 'cache')
                self.assertFalse(first['ocr']['cache_hit']); self.assertTrue(second['ocr']['cache_hit'])
                self.assertEqual(run.call_count, 1)
                settings['model_sha256'] = 'second'
                third = ocr.with_ocr_fallback('local.pdf', data, folder / 'cache')
                self.assertFalse(third['ocr']['cache_hit'])
                self.assertEqual(run.call_count, 2)
            self.assertEqual(data, original)
            self.assertEqual(first['pages'][0]['text'], data['pages'][0]['text'])
            rows = reconcile_rows(bounded_evidence(first)['entries'], bounded=bounded_evidence(first),
                                  evidence=first, source_sha256='a' * 64)
            self.assertTrue(rows[0]['needs_review'])
            self.assertIn('ocr_source_requires_review', rows[0]['warnings'])
            self.assertEqual(authenticated_sources(rows, 'a' * 64)[1]['page'], 2)

    def test_timeout_is_logged_without_replacing_the_native_evidence(self):
        data = native([''])
        data.update(evidence_path='native.json', evidence_sha256='b' * 64)
        with tempfile.TemporaryDirectory() as folder, patch.object(ocr, 'ocr_settings', return_value={'version': ocr.VERSION}), \
                patch.object(ocr.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ocr', 60)):
            result = ocr.with_ocr_fallback('local.pdf', data, folder)
        self.assertEqual(result['pages'], data['pages'])
        self.assertEqual(result['ocr']['status'], 'failed')
        self.assertEqual(result['ocr']['error'], 'TimeoutExpired')

    def test_detached_labels_follow_their_column_not_a_separate_number_column(self):
        lines = []
        for number, x, top in ((21, 10, 100), (22, 10, 140), (23, 10, 180),
                               (24, 210, 100), (25, 210, 140), (26, 210, 180)):
            lines += [{'text': f'{number}.', 'bbox': [x, top, x + 15, top + 14]},
                      {'text': f'A. Lee, "Title {number}," 2020.', 'bbox': [x + 23, top, x + 180, top + 16]}]
        text, joined = ocr.ocr_reading_text({'text': 'unreliable column order', 'lines': lines}, 1000)
        self.assertEqual([int(line.split('.')[0]) for line in text.splitlines()], [21, 22, 23, 24, 25, 26])
        self.assertEqual(len(joined), 6)

    def test_geometry_cannot_invent_or_move_an_unreadable_label(self):
        lines = [{'text': '2%', 'bbox': [10, 100, 25, 114]},
                 {'text': 'A. Lee, "Title," 2020.', 'bbox': [33, 100, 180, 116]},
                 {'text': '22.', 'bbox': [10, 200, 25, 214]}]
        text, joined = ocr.ocr_reading_text({'text': '2%\nA. Lee, "Title," 2020.\n22.', 'lines': lines}, 1000)
        self.assertFalse(joined)
        self.assertNotIn('21.', text)

    def test_bulk_text_first_success_never_launches_mineru(self):
        for image_only in (False, True):
            data = native(['References\n[1] A. Lee, "Actual title," 2020.'])
            data.update(evidence_path='evidence.json', evidence_sha256='b' * 64,
                        ocr={'status': 'complete' if image_only else 'not_needed'})
            if image_only:
                data['extractor'] = ocr.EXTRACTOR
            pages = [(1, '' if image_only else data['pages'][0]['text'])]
            with tempfile.TemporaryDirectory() as folder:
                out = Path(folder)
                bulk.save(out / 'manifest.json', {'papers': [
                    {'id': 'paper', 'pdf': 'local.pdf', 'sha256': 'a' * 64, 'bytes': 100}]})
                with patch.object(bulk, 'digest_file', return_value='a' * 64), \
                        patch.object(bulk, 'pdf_pages', return_value=pages), \
                        patch.object(bulk, 'pdf_evidence', return_value=data), \
                        patch.object(bulk, 'MinerWorker') as miner:
                    bulk.prepare(out)
                miner.assert_not_called()
                result = bulk.read(out / 'papers/paper/prepared.json')
                self.assertEqual(result['status'], 'ready')
                self.assertEqual(len(result['entries']), 1)
                self.assertTrue(result['mineru_skipped'])
                self.assertEqual(result['entry_source'], 'bounded_pdf_ocr' if image_only else 'native_text')


if __name__ == '__main__':
    unittest.main()
