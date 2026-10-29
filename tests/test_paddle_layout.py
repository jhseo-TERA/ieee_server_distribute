import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from paper_server.parsers.paddle_layout import PaddleLayoutStore, pixel_box_to_pdf, valid_pages
from paper_server.serdes.figures import find_candidates, scan_document


class PaddleFigureGeometryTests(unittest.TestCase):
    def setUp(self):
        self.caption = {'text': 'Fig. 2. Proposed receiver architecture.', 'box': [40, 100, 290, 108]}
        self.region = {'label': 'image', 'confidence': .96, 'bbox': [38, 120, 291, 360]}

    def test_detector_recovers_full_diagram_from_fragmented_pdf_image(self):
        # A native object contains only a subpanel. The caption-bound detector
        # recovers the complete diagram but never marks its choice verified.
        result = find_candidates([self.caption], [[140, 125, 215, 200]], 600, 800, 2,
                                 layouts=[self.region])
        self.assertEqual(result[0]['bbox'], [33, 115, 296, 365])
        self.assertEqual(result[0]['method'], 'paddle_layout')
        self.assertEqual(result[0]['layout_detection']['confidence'], .96)
        self.assertIn('paddle_layout_review', result[0]['review_reasons'])
        self.assertNotIn('verified', result[0])

    def test_detector_never_promotes_arbitrary_plot_or_uncaptioned_image(self):
        for caption in ([], [{'text': 'Fig. 2. Measured eye diagram.', 'box': [40, 100, 290, 108]}],
                        [{'text': 'Fig. 2 shows the receiver architecture.', 'box': [40, 100, 290, 108]}]):
            self.assertEqual(find_candidates(caption, [], 600, 800, 2, layouts=[self.region]), [])
        for region in ({**self.region, 'label': 'table'}, {**self.region, 'confidence': .59},
                       {**self.region, 'bbox': [320, 120, 580, 360]},
                       {**self.region, 'bbox': [40, 280, 291, 360]},
                       {**self.region, 'bbox': [0, 0, 600, 800]}):
            self.assertEqual(find_candidates([self.caption], [], 600, 800, 2, layouts=[region]), [])

    def test_native_fallback_and_semantic_panel_review_remain(self):
        native = [[40, 125, 290, 360]]
        result = find_candidates([self.caption], native, 600, 800, 2, layouts=[])
        self.assertEqual(result[0]['method'], 'graphic_object')
        mixed = {**self.caption, 'text': 'Fig. 2. (a) Proposed RX architecture and (b) eye diagrams.'}
        result = find_candidates([mixed], native, 600, 800, 2, layouts=[self.region])
        self.assertIn('multi_panel', result[0]['review_reasons'])
        self.assertIn('paddle_layout_review', result[0]['review_reasons'])

    def test_padding_excludes_wrapped_previous_caption_without_cutting_detector_region(self):
        # Measured geometry from 9830507 p2: the final word of Figure 1's
        # caption is only 1.67pt above the detected Figure 2 architecture.
        caption = {'text':'Figure 2. Detailed block diagram of 1DQ RX/TX and the connection with memory host.',
                   'box':[48.4, 275.8, 297.5, 284.11]}
        tail = {'text':'coil.', 'box':[48.574, 485.781, 64.104, 492.229]}
        region = {'label':'image', 'confidence':.94079, 'bbox':[48.208, 290.989, 295.437, 484.113]}
        result = find_candidates([tail, caption], [], 612, 792, 2, layouts=[region])[0]
        self.assertGreaterEqual(result['bbox'][3], region['bbox'][3])
        self.assertLess(result['bbox'][3], tail['box'][1])
        self.assertEqual(result['bbox'][3], 484.78)
        native = find_candidates([tail, caption], [region['bbox']], 612, 792, 2)[0]
        self.assertEqual(native['bbox'][3], 489.11)

    def test_unrotated_raster_maps_to_bottom_left_local_pdf_coordinates(self):
        self.assertEqual(pixel_box_to_pdf([20., 40., 60., 120.], 200, 400, 100, 200), [10, 140, 30, 180])
        for box in ([0, 0, float('nan'), 30], [-3, 0, 20, 30], [0, 0, 300, 30], [0, 30, 20, 20]):
            with self.assertRaises(ValueError):
                pixel_box_to_pdf(box, 200, 400, 100, 200)

    def test_geometry_mismatch_does_not_reuse_cached_region(self):
        page = Mock()
        page.get_bbox.return_value = (0, 0, 600, 800)
        page.get_textpage.return_value.get_text_range.return_value = ''
        page.get_objects.return_value = []
        diagnostics = []
        with patch('paper_server.serdes.figures.recover_caption_lines', return_value=[self.caption]):
            result = scan_document([page], diagnostics=diagnostics,
                                   layout_pages={'1': {'width': 600, 'height': 801, 'regions': [self.region]}})
        self.assertEqual(result, [])
        self.assertTrue(any(d['reason'] == 'layout_geometry_mismatch' for d in diagnostics))


class PaddleCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pdf = self.root / 'test.pdf'
        self.pdf.write_bytes(b'%PDF-layout-test')
        self.digest = hashlib.sha256(self.pdf.read_bytes()).hexdigest()
        self.store = PaddleLayoutStore(self.root)
        self.store.python = self.root / 'python.exe'
        self.store.python.touch()
        self.store.model_dir = self.root / 'model'
        self.store.model_dir.mkdir()
        (self.store.model_dir / 'inference.yml').write_text('model: test')
        self.pages = {'1': {'width': 600, 'height': 800, 'regions': [
            {'label': 'image', 'confidence': .95, 'bbox': [40, 120, 290, 360]}]}}

    def run_worker(self, command, **kwargs):
        output = Path(command[command.index('--output') + 1])
        output.write_text(json.dumps({'pdf_sha256': self.digest, 'model': self.store.model_name,
                                      'pages': self.pages, 'provenance': {'paddleocr_version': 'test'}}))
        return Mock(returncode=0)

    def test_source_and_model_bound_cache_skips_second_inference(self):
        with patch('paper_server.parsers.paddle_layout.subprocess.run', side_effect=self.run_worker) as worker:
            self.assertEqual(self.store.prepare(self.pdf, self.digest)['status'], 'ready')
            self.assertTrue(self.store.prepare(self.pdf, self.digest)['cached'])
            self.assertEqual(worker.call_count, 1)
            (self.store.model_dir / 'inference.yml').write_text('changed model configuration')
            self.assertFalse(self.store.prepare(self.pdf, self.digest)['cached'])
            self.assertEqual(worker.call_count, 2)

    def test_missing_model_never_downloads_or_launches_worker(self):
        self.store.model_dir = self.root / 'unprovisioned-model'
        with patch('paper_server.parsers.paddle_layout.subprocess.run') as worker:
            result = self.store.prepare(self.pdf, self.digest)
        self.assertEqual(result['status'], 'model_unavailable')
        worker.assert_not_called()

    def test_package_upgrade_invalidates_layout_cache(self):
        with patch.object(self.store, 'runtime_versions', return_value={'paddleocr': '3.7.0'}), \
                patch('paper_server.parsers.paddle_layout.subprocess.run', side_effect=self.run_worker) as worker:
            self.store.prepare(self.pdf, self.digest)
            self.assertTrue(self.store.prepare(self.pdf, self.digest)['cached'])
        with patch.object(self.store, 'runtime_versions', return_value={'paddleocr': '3.8.0'}), \
                patch('paper_server.parsers.paddle_layout.subprocess.run', side_effect=self.run_worker) as worker:
            self.assertFalse(self.store.prepare(self.pdf, self.digest)['cached'])
            worker.assert_called_once()

    def test_timeout_or_corrupt_output_falls_back_without_cache(self):
        import subprocess
        with patch('paper_server.parsers.paddle_layout.subprocess.run', side_effect=subprocess.TimeoutExpired('test', 180)):
            result = self.store.prepare(self.pdf, self.digest)
            self.assertEqual(result['status'], 'timeout')
            self.assertTrue((self.root / result['diagnostic_file']).is_file())
        self.pages['1']['regions'][0]['bbox'] = [0, 0, float('nan'), 200]
        with patch('paper_server.parsers.paddle_layout.subprocess.run', side_effect=self.run_worker):
            self.assertEqual(self.store.prepare(self.pdf, self.digest)['status'], 'inference_failed')
        self.assertEqual(list(self.store.directory.glob('*.json')), [])

    def test_untrusted_cache_rejects_out_of_page_and_nonfinite_data(self):
        self.assertTrue(valid_pages(self.pages, 24))
        for field, value in (('confidence', float('nan')), ('confidence', 2), ('bbox', [0, 0, 601, 400])):
            pages = json.loads(json.dumps(self.pages))
            pages['1']['regions'][0][field] = value
            self.assertFalse(valid_pages(pages, 24))


if __name__ == '__main__':
    unittest.main()
