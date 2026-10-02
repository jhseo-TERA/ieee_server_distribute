import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from serdes_ocr import recognize_page, valid_lines


class LocalOcrTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.directory=self.root/'outputs/serdes_ocr'
        self.directory.mkdir(parents=True)
        self.page=Mock();self.page.get_bbox.return_value=(7,-36,607,764)
        self.page.get_rotation.return_value=90
        self.bitmap=self.page.render.return_value
        self.bitmap.width=1500;self.bitmap.height=2000
        self.node=patch('serdes_ocr.shutil.which',return_value='node')
        self.node.start();self.addCleanup(self.node.stop)

    def model(self):
        (self.directory/'eng.traineddata').write_bytes(b'test-model')

    def test_no_model_never_starts_network_or_process(self):
        with patch('serdes_ocr.subprocess.run') as run:
            self.assertEqual(recognize_page(self.page,self.root,'digest',0),([], 'ocr_unavailable'))
        run.assert_not_called();self.page.render.assert_not_called()

    def test_recognition_coordinates_are_page_local_and_rotation_is_cancelled(self):
        self.model()
        result=Mock(stdout=json.dumps({'lines':[{'text':'Fig. 1. RX architecture.','box':[100,200,500,240]}]}))
        with patch('serdes_ocr.subprocess.run',return_value=result) as run:
            lines,status=recognize_page(self.page,self.root,'digest',0)
        self.assertEqual(status,'ocr_recognized')
        self.assertEqual(lines[0]['box'],[40,704,200,720])
        self.assertEqual(self.page.render.call_args.kwargs['rotation'],270)
        self.assertEqual(run.call_args.kwargs['timeout'],45)
        self.bitmap.close.assert_called_once()

    def test_valid_cache_is_reused_without_rendering(self):
        self.model();lines=[{'text':'Fig. 1. RX.','box':[10,20,100,40]}]
        (self.directory/'digest-1-v1.json').write_text(json.dumps({'lines':lines}))
        self.assertEqual(recognize_page(self.page,self.root,'digest',0),(lines,'ocr_cached'))
        self.page.render.assert_not_called()

    def test_corrupt_cache_and_timeout_fail_without_aborting_document(self):
        self.model();(self.directory/'digest-1-v1.json').write_text('{invalid')
        with patch('serdes_ocr.subprocess.run',side_effect=subprocess.TimeoutExpired('node',45)):
            self.assertEqual(recognize_page(self.page,self.root,'digest',0),([], 'ocr_failed'))
        self.bitmap.close.assert_called_once()

    def test_malformed_boxes_are_not_accepted(self):
        self.assertFalse(valid_lines([{'text':'Fig. 1','box':[0,0,float('nan'),3]}]))
        self.assertFalse(valid_lines([{'text':'Fig. 1','box':[0,0,3]}]))
        self.model()
        with patch('serdes_ocr.subprocess.run',return_value=Mock(stdout='{"lines":null}')):
            self.assertEqual(recognize_page(self.page,self.root,'digest',0),([], 'ocr_failed'))


if __name__=='__main__':unittest.main()
