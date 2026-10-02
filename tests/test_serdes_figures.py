import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from serdes_figures import (FigureStore, VERSION, STORAGE_VERSION, caption_score, find_candidates,
                           text_lines, merge_tiles, page_rect, render_crop, recover_caption_lines, scan_document)
import web.app as app_module


class FigureSelectionTests(unittest.TestCase):
    def test_reviewed_irregular_panels_preserve_pixels_and_positions(self):
        from PIL import Image
        from serdes_figures import isolate_reviewed_regions
        source = Image.new('RGB',(100,80),'red')
        result = isolate_reviewed_regions(source,[10,20,110,100],[[10,20,110,60],[10,60,50,100]])
        self.assertEqual(result.size,source.size)
        for point in ((10,10),(10,60),(90,60)):
            self.assertEqual(result.getpixel(point),source.getpixel(point))
        self.assertEqual(result.getpixel((90,10)),(255,255,255))

    def test_reviewed_irregular_panels_reject_invalid_regions(self):
        from PIL import Image
        from serdes_figures import isolate_reviewed_regions
        from local_ai_documents import DocumentError
        source=Image.new('RGB',(100,100))
        for regions in ([],[[0,0,101,100]],[[10,0,0,100]],[[0,0,float('nan'),100]],[[0,0,'50',100]]):
            with self.assertRaises(DocumentError):
                isolate_reviewed_regions(source,[0,0,100,100],regions)
        with self.assertRaises(DocumentError):
            isolate_reviewed_regions(source,[0,0,100,100],[[0,0,100,100]],90)

    def test_caption_without_final_number_punctuation_and_prose_rejection(self):
        for label in ('Fig. 1 Receiver architecture.', 'Figure 1: Receiver architecture.', 'Fig.1.Receiver architecture.'):
            self.assertEqual(len(find_candidates([{'text':label,'box':[40,100,290,108]}],[[40,125,290,360]],600,800,1)),1,label)
        for label in ('Fig. 1 shows the receiver architecture.', 'Fig.1. shows the receiver architecture.', 'Fig. 1 is the receiver architecture.'):
            self.assertEqual(find_candidates([{'text':label,'box':[40,100,290,108]}],[[40,125,290,360]],600,800,1),[],label)

    def test_structure_topology_and_driver_overview_retrieval(self):
        for label in ('Receiver structure', 'DAC Driver overview', 'BM-RX AFE topology', 'System block diagram'):
            self.assertGreaterEqual(caption_score('Fig. 1. '+label,'1'),14,label)
        self.assertEqual(caption_score('Fig. 1. Measurement setup with receiver structure.','1'),0)
        self.assertEqual(caption_score('Fig. 6. Overall simulated result with different TIS architecture. (a) Transimpedance gain and (b) noise contributions.','6'),0)

    def test_combined_caption_objects_are_recovered_separately(self):
        tp=Mock();tp.get_text_range.return_value='Fig. 1. RX architecture. Fig. 2. Measured eyes.'
        tp.search.return_value.get_next.return_value=(0,2)
        tp.get_charbox.side_effect=[(40,100,50,108),(50,100,290,108),(310,100,320,108),(320,100,560,108)]
        recovered=recover_caption_lines(tp,[{'text':tp.get_text_range.return_value,'box':[40,100,560,108]}],[0,0,600,800])
        self.assertEqual([x['text'] for x in recovered],['Fig. 1. RX architecture.','Fig. 2. Measured eyes.'])
        self.assertLess(recovered[0]['box'][2],recovered[1]['box'][0])

    def test_caption_regressions_from_reviewed_papers(self):
        for caption in ('Top-level overview of backplane signal conditioner.',
                        'Top architecutre of the proposed dual-path CDR.',
                        'Proposed optical RX block diagram. Insets show the CTLE and VGA stages.',
                        'ADC-based PAM-4 receiver with CTLE front end, 6-bit SAR ADC, DSP, and CDR.'):
            self.assertGreaterEqual(caption_score('Fig. 2. '+caption,'2'),14,caption)
        self.assertEqual(caption_score('Fig. 4. Block diagram of the experiment.','4'),0)
        self.assertLess(caption_score('Fig. 1. Target backplane architecture and channel characteristics.','1'),14)
        self.assertEqual(caption_score('Fig. 1. TX architecture.','1'),caption_score('Fig. 14. TX architecture.','14'))

    def test_three_vertical_tiles_are_joined_but_nearby_panels_are_not(self):
        tiles=[[100,100,300,160],[100,160,300,220],[100,220,300,280]]
        self.assertEqual(merge_tiles(tiles),[[100,100,300,280]])
        unrelated=[tiles[0],[100,170,300,230],[305,100,500,160]]
        self.assertCountEqual(merge_tiles(unrelated),unrelated)

    def test_fragmented_native_caption_recovery_and_deduplication(self):
        tp=Mock()
        tp.get_text_range.return_value='Fig. 2. Receiver architecture.\nNot a caption.'
        tp.search.return_value.get_next.return_value=(0,2)
        tp.get_charbox.side_effect=[(17,64,27,72),(27,64,87,72)]
        recovered=recover_caption_lines(tp,[],[7,-36,607,764])
        self.assertEqual(recovered,[{'text':'Fig. 2. Receiver architecture.','box':[10,100,80,108]}])
        tp.search.return_value.close.assert_called_once()
        tp.search.reset_mock()
        self.assertEqual(recover_caption_lines(tp,recovered,[7,-36,607,764]),recovered)
        tp.search.assert_not_called()

    def test_vector_fallback_is_review_only(self):
        line={'text':'Fig. 2. Proposed receiver architecture.','box':[40,100,290,108]}
        paths=[[40+i*12,120,45+i*12,180] for i in range(15)]
        candidate=find_candidates([line],[],600,800,2,paths=paths)[0]
        self.assertIn('vector_region_review',candidate['review_reasons'])
        self.assertEqual(find_candidates([line],[],600,800,2,paths=paths[:3]),[])

    def test_ocr_is_bounded_to_three_pages_per_document(self):
        pages=[]
        for _ in range(8):
            page=Mock();page.get_bbox.return_value=(0,0,600,800)
            page.get_textpage.return_value.get_text_range.return_value=''
            page.get_objects.return_value=[];pages.append(page)
        ocr=Mock(return_value=([],'ocr_unavailable'))
        self.assertEqual(scan_document(pages,diagnostics=[],ocr=ocr),[])
        self.assertEqual(ocr.call_count,3)

    def test_pdf_origin_and_every_rotation(self):
        self.assertEqual(page_rect([17,4,37,24],[7,-36,607,764]),[10,40,30,60])
        self.assertEqual(render_crop([10,20,30,60],100,200,0),(10,20,70,140))
        self.assertEqual(render_crop([10,20,30,60],100,200,90),(20,70,140,10))
        self.assertEqual(render_crop([10,20,30,60],100,200,180),(70,140,10,20))
        self.assertEqual(render_crop([10,20,30,60],100,200,270),(140,10,20,70))

    def test_mixed_panel_is_not_treated_as_verified_architecture(self):
        line={'text':'Fig. 4. (a) Proposed RX architecture and (b) eye diagrams.','box':[40,100,290,108]}
        candidate=find_candidates([line],[[40,125,290,360]],600,800,2)[0]
        self.assertIn('multi_panel',candidate['review_reasons'])

    def test_rejected_captions_remain_in_diagnostics(self):
        trace=[]
        find_candidates([{'text':'Fig. 1. Measured eye diagram.','box':[40,100,290,108]}],[],600,800,1,diagnostics=trace)
        self.assertEqual(trace[0]['reason'],'below_threshold')
        self.assertEqual(trace[0]['score'],0)

    def test_main_receiver_ranks_above_featured_afe_and_conventional_design(self):
        title = 'A PAM-4 Receiver Featuring Current-Reuse AFE'
        main = caption_score('Fig. 1. Architecture of the proposed PAM-4 receiver.', '1', title)
        afe = caption_score('Fig. 2. Block diagram of the proposed AFE.', '2', title)
        previous = caption_score('Fig. 1. Block diagram of a conventional receiver.', '1', title)
        self.assertGreater(main, afe)
        self.assertGreater(main, previous)
        self.assertEqual(caption_score('Fig. 1. Measured eye diagram.', '1'), 0)
        self.assertEqual(caption_score('Fig. 3. Block diagram of the measurement setup to investigate the receiver.', '3'), 0)

    def test_front_end_title_prefers_overall_afe_to_internal_differentiator(self):
        title = 'PAM-4 Receiver Front-End With Crosstalk Cancellation'
        self.assertGreater(caption_score('Fig. 11. Proposed AFE architecture.', '11', title),
                           caption_score('Fig. 13. Architecture of the proposed differentiator.', '13', title))

    def test_adjacent_column_captions_are_not_merged(self):
        lines = text_lines([
            {'text': 'Figure 23.5.1: Proposed TX architecture.', 'box': [40,100,296,107]},
            {'text': 'Figure 23.5.2: CTLE schematic.', 'box': [302,100,560,107]},
        ])
        self.assertEqual(len(lines), 2)
        self.assertNotIn('23.5.2', lines[0]['text'])

    def test_figure_requires_explicit_caption_and_actual_graphic_above_it(self):
        caption = {'text': 'Fig. 2. Top-level architecture of the proposed RX.', 'box': [40,100,290,108]}
        graphic = [40,125,290,360]
        result = find_candidates([caption], [graphic], 600, 800, 2)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['bbox'], [35,120,295,365])
        self.assertEqual(result[0]['page'], 2)
        for graphics in ([], [[40,10,290,80]], [[0,0,600,800]], [[40,200,290,400]]):
            self.assertEqual(find_candidates([caption], graphics, 600, 800, 2), [])
        prose = {**caption, 'text': 'Fig. 2 shows the architecture of the receiver.'}
        self.assertEqual(find_candidates([prose], [graphic], 600, 800, 2), [])

    def test_multi_line_caption_is_retained(self):
        lines = [{'text':'Fig. 1. (a) Conventional receiver and', 'box':[40,100,290,108]},
                 {'text':'(b) proposed receiver architecture.', 'box':[40,90,250,98]}]
        result = find_candidates(lines, [[40,125,290,360]], 600,800,2)
        self.assertEqual(len(result),1)
        self.assertIn('(b) proposed',result[0]['caption'])


class FigureStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'ieee-pdf').mkdir()
        self.pdf = self.root/'ieee-pdf'/'123.pdf'
        self.pdf.write_bytes(b'%PDF-read-only-test')
        self.paper = {'article_number':'123','source_system':'ieee','pdf_local_path':'ieee-pdf/123.pdf'}
        self.store = FigureStore(self.root)
        self.folder = self.store.paper_directory('123')
        self.folder.mkdir(parents=True)
        self.record = {'version':VERSION,'fingerprint':self.store.fingerprint(self.pdf),'status':'available',
                       'image':'a'*32+'.png','page':2,'figure_number':'1','caption':'Fig. 1. RX architecture.',
                       'width':800,'height':400}
        (self.folder/self.record['image']).write_bytes(b'cached-test-image')
        self.write_record()

    def write_record(self):
        (self.folder/'index.json').write_text(json.dumps(self.record),encoding='utf-8')

    def absence(self):
        import hashlib
        return {'disposition':'no_top_diagram','page_count':3,'page':2,
                'source_pages_reviewed':[1,2,3],'pdf_sha256':hashlib.sha256(self.pdf.read_bytes()).hexdigest(),
                'evidence':'All pages reviewed; only measured curves.', 'absence_reason':'plots_only',
                'review_note_ko':'원문에는 측정 그래프만 있습니다.'}

    def test_confirmed_absence_has_pdf_link_but_no_image_or_hover_parsing(self):
        self.record={**self.record,**self.absence(),'status':'no_top_diagram','verified':True,'selection':'source_review'}
        del self.record['image'];self.write_record()
        with patch('pypdfium2.PdfDocument',side_effect=AssertionError('No parsing on hover')):
            result=self.store.public(self.paper,{'id':1,'component_scope':'rx'})
        self.assertEqual(result['status'],'no_top_diagram')
        self.assertTrue(result['verified']);self.assertEqual(result['pdf_url'],'/pdf/123#page=2')
        self.assertIn('측정',result['review_note']);self.assertNotIn('image_url',result)
        self.assertIsNone(self.store.image_path(self.paper))

    def test_absence_requires_every_page_and_source_hash(self):
        (self.root/'config').mkdir()
        entry=self.absence()
        for field,value in [('page_count',4),('source_pages_reviewed',[1,3]),('evidence',''),('absence_reason',''),('page',4)]:
            with self.subTest(field=field):
                self.store.review_path.write_text(json.dumps({'123':{**entry,field:value}}))
                with self.assertRaisesRegex(ValueError,'absence review'):
                    self.store.reviewed_selection(self.paper,entry['pdf_sha256'])
        self.store.review_path.write_text(json.dumps({'123':entry}))
        self.assertTrue(self.store.reviewed_selection(self.paper,entry['pdf_sha256'])['verified'])
        with self.assertRaisesRegex(ValueError,'hash changed'):
            self.store.reviewed_selection(self.paper,'different')

    def test_absence_prepare_checks_real_page_count_and_preserves_prior_image(self):
        (self.root/'config').mkdir();entry=self.absence()
        self.store.review_path.write_text(json.dumps({'123':entry}))
        with patch('pypdfium2.PdfDocument') as doc, patch('serdes_figures.scan_document') as scan:
            doc.return_value.__enter__.return_value.__len__.return_value=2
            with self.assertRaisesRegex(ValueError,'actual complete document'):
                self.store.prepare(self.paper,retry=True)
            doc.return_value.__enter__.return_value.__len__.return_value=3
            result=self.store.prepare(self.paper,retry=True)
            self.assertEqual(result['status'],'no_top_diagram');scan.assert_not_called()
        self.assertTrue((self.folder/self.record['image']).exists())
        self.assertEqual(self.store.lookup(self.paper)['status'],'no_top_diagram')
        self.pdf.write_bytes(b'%PDF-changed')
        self.assertEqual(self.store.lookup(self.paper)['status'],'not_prepared')

    def test_reviewed_scope_reference_is_explicitly_not_a_verified_measurement_link(self):
        self.record.update(scope='afe',verified=True,selection='source_review',evidence='Complete AFE only')
        self.write_record()
        for scope in ('rx','tx_rx','unknown','cdr','new_scope'):
            result=self.store.public(self.paper,{'id':1,'component_scope':scope})
            self.assertEqual(result['status'],'reference_only')
            self.assertFalse(result['association_verified']);self.assertTrue(result['verified'])
            self.assertEqual(result['scope'],'afe');self.assertIn('image_url',result)
        # A different implementation cannot be smuggled through as a reference.
        self.record['implementation_ids']=[4];self.write_record()
        result=self.store.public(self.paper,{'id':1,'implementation_id':5,'component_scope':'rx'})
        self.assertEqual(result['status'],'review_required');self.assertNotIn('image_url',result)

    def test_unknown_measurement_scope_never_claims_direct_association(self):
        self.record.update(scope='rx');self.write_record()
        self.assertEqual(self.store.public(self.paper,{'id':1,'component_scope':'unknown'})['status'],'review_required')

    def test_variant_note_is_source_bound_even_when_scope_matches(self):
        self.record.update(scope='rx',verified=True,selection='source_review',evidence='Two implementations',
                           pdf_sha256='a'*64,bbox=[1,2,100,200]);self.write_record()
        (self.root/'config').mkdir()
        note={k:self.record[k] for k in ('pdf_sha256','page','figure_number','scope','bbox')}
        note['note']='A와 B는 다른 구현입니다.'
        (self.root/'config/serdes_figure_context_notes.json').write_text(json.dumps({'123':note}),encoding='utf-8')
        result=self.store.public(self.paper,{'id':1,'component_scope':'rx'})
        self.assertEqual(result['status'],'reference_only');self.assertEqual(result['scope_note'],note['note'])
        self.record['bbox']=[2,3,101,201];self.write_record()
        result=self.store.public(self.paper,{'id':1,'component_scope':'rx'})
        self.assertIn('변경',result['scope_note']);self.assertFalse(result['association_verified'])

    def test_lookup_does_not_parse_pdf_and_does_not_expose_local_paths(self):
        with patch('pypdfium2.PdfDocument', side_effect=AssertionError('No PDF parsing during hover')):
            result = self.store.public(self.paper)
        self.assertEqual(result['status'],'available')
        self.assertFalse(result['verified'])
        self.assertEqual(result['pdf_url'],'/pdf/123#page=2')
        self.assertNotIn('fingerprint',result)
        self.assertNotIn(str(self.root),json.dumps(result))

    def test_changed_source_invalidates_image_without_removing_old_files(self):
        self.pdf.write_bytes(b'%PDF-new-source-content')
        self.assertEqual(self.store.lookup(self.paper)['status'],'not_prepared')
        self.assertIsNone(self.store.image_path(self.paper))
        self.assertTrue((self.folder/self.record['image']).exists())

    def test_missing_pdf_and_missing_index_are_distinct(self):
        self.assertEqual(self.store.lookup({**self.paper,'pdf_local_path':'ieee-pdf/missing.pdf'})['status'],'missing_pdf')
        self.assertEqual(self.store.lookup({**self.paper,'article_number':'other'})['status'],'not_prepared')

    def test_malformed_metadata_and_traversal_fail_closed(self):
        for field,value in [('image','../../private.png'),('page',-1),('width',0),('status','invented'),('caption',None)]:
            with self.subTest(field=field):
                old=self.record[field];self.record[field]=value;self.write_record()
                self.assertEqual(self.store.lookup(self.paper)['status'],'not_prepared')
                self.record[field]=old
        with self.assertRaises(ValueError):
            self.store.paper_directory('../private')
        self.assertEqual(self.store.lookup({**self.paper,'pdf_local_path':'../private.pdf'})['status'],'missing_pdf')

    def test_prepared_cache_is_reused_without_rendering_or_writes(self):
        before=(self.folder/'index.json').read_bytes()
        with patch('pypdfium2.PdfDocument',side_effect=AssertionError('Should reuse prepared image')):
            self.assertEqual(self.store.prepare(self.paper)['status'],'available')
        self.assertEqual((self.folder/'index.json').read_bytes(),before)

    def test_legacy_cache_remains_readable_but_is_rebuilt_on_prepare(self):
        self.record['version']=STORAGE_VERSION;self.write_record()
        self.assertEqual(self.store.public(self.paper)['status'],'available')
        with patch('pypdfium2.PdfDocument') as document, patch('serdes_figures.scan_document',return_value=[]):
            document.return_value.__enter__.return_value.__len__.return_value=1
            result=self.store.prepare(self.paper)
        self.assertEqual(result['version'],VERSION)
        self.assertEqual(result['status'],'not_found')
        self.assertTrue((self.folder/self.record['image']).exists())

    def test_different_measurement_or_scope_never_receives_reviewed_image(self):
        self.record.update(measurement_ids=[2180],scope='rx',verified=True,selection='source_review');self.write_record()
        self.assertEqual(self.store.public(self.paper,{'id':2180,'component_scope':'rx'})['measurement_link'],'reviewed')
        result=self.store.public(self.paper,{'id':2181,'component_scope':'rx'})
        self.assertEqual(result['status'],'review_required');self.assertNotIn('image_url',result)
        result=self.store.public(self.paper,{'id':2180,'component_scope':'trx'})
        self.assertEqual(result['review_reasons'],['scope_mismatch'])

    def test_source_hash_mismatch_cannot_use_reviewed_override(self):
        (self.root/'config').mkdir()
        self.store.review_path.write_text(json.dumps({'123':{'pdf_sha256':'wrong'}}),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'hash changed'):
            self.store.reviewed_selection(self.paper,'actual')

    def test_review_candidates_expose_reason_not_an_image(self):
        self.record.update(status='review_required',review_reasons=['multi_panel']);self.write_record()
        result=self.store.public(self.paper)
        self.assertEqual(result['review_reasons'],['multi_panel']);self.assertNotIn('image_url',result)
        self.assertIsNone(self.store.image_path(self.paper))

    def test_implementation_binding_allows_only_reviewed_component_and_normalizes_trx(self):
        self.record.update(implementation_ids=[9],scope='driver',verified=True);self.write_record()
        self.assertEqual(self.store.public(self.paper,{'id':1,'implementation_id':9,'component_scope':'tx'})['status'],'available')
        self.assertEqual(self.store.public(self.paper,{'id':1,'implementation_id':8,'component_scope':'tx'})['status'],'review_required')
        self.record.update(scope='trx');self.write_record()
        self.assertEqual(self.store.public(self.paper,{'id':1,'implementation_id':9,'component_scope':'tx_rx'})['status'],'available')

    def test_full_link_measurement_cannot_use_tx_only_image(self):
        self.record.update(scope='tx');self.write_record()
        data=self.store.public(self.paper,{'id':1,'component_scope':'full_link'})
        self.assertEqual(data['review_reasons'],['scope_mismatch'])
        self.assertNotIn('image_url',data)

    def test_unknown_and_auxiliary_scopes_do_not_bypass_measurement_guard(self):
        for scope in ('unknown','clock','detector','dsp','equalizer','demux','trx_afe','io','redriver','future_scope'):
            for requested in ('tx','rx','trx','tx_rx','full_link'):
                with self.subTest(scope=scope,requested=requested):
                    self.record.update(scope=scope,verified=True,implementation_ids=[9])
                    self.write_record()
                    result=self.store.public(self.paper,{'id':1,'implementation_id':9,'component_scope':requested})
                    self.assertEqual(result['review_reasons'],['scope_mismatch'])
                    self.assertNotIn('image_url',result)
            self.assertEqual(self.store.public(self.paper)['status'],'available')

    def test_review_flags_are_source_bound_and_missing_figure_keeps_pdf_link(self):
        (self.root/'config').mkdir()
        self.store.flags_path.write_text(json.dumps({'123':{'pdf_sha256':'abc','reasons':['crop_contamination']}}))
        self.assertEqual(self.store.review_flags('123','abc'),['crop_contamination'])
        self.assertEqual(self.store.review_flags('123','changed'),['review_source_changed'])
        self.record={k:v for k,v in self.record.items() if k not in ('image','page')}
        self.record['status']='not_found';self.write_record()
        self.assertEqual(self.store.public(self.paper)['pdf_url'],'/pdf/123#page=1')


class FigureRouteTests(unittest.TestCase):
    def setUp(self):
        self.client=app_module.app.test_client()
        app_module.app.config['TESTING']=True

    def test_routes_require_login(self):
        for path in ('/api/serdes/papers/123/block-diagram','/api/serdes/papers/123/block-diagram/image'):
            self.assertEqual(self.client.get(path).status_code,401)

    def login(self):
        with self.client.session_transaction() as session:
            session.update(authenticated=True,username='viewer',role='viewer')

    def test_viewer_can_read_cached_figure_but_no_preparation_is_triggered(self):
        self.login()
        with patch.object(app_module,'_diagram_paper',return_value={'article_number':'123'}), \
                patch.object(app_module.serdes_figures,'public',return_value={'status':'not_prepared'}), \
                patch.object(app_module.serdes_figures,'prepare') as prepare:
            result=self.client.get('/api/serdes/papers/123/block-diagram')
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.headers['Cache-Control'],'private, no-store')
        prepare.assert_not_called()

    def test_image_is_private_and_old_version_url_is_rejected(self):
        self.login()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/('a'*32+'.png');path.write_bytes(b'PNG-test')
            with patch.object(app_module,'_diagram_paper',return_value={'article_number':'123'}), \
                    patch.object(app_module.serdes_figures,'image_path',return_value=path):
                result=self.client.get('/api/serdes/papers/123/block-diagram/image?v='+'a'*32)
                self.assertEqual(result.status_code,200)
                self.assertEqual(result.mimetype,'image/png')
                self.assertEqual(result.headers['Cache-Control'],'private, no-cache')
                result.close()
                self.assertEqual(self.client.get('/api/serdes/papers/123/block-diagram/image?v=old').status_code,404)

    def test_measurement_id_must_be_valid_and_belong_to_this_paper(self):
        self.login()
        with patch.object(app_module,'_diagram_paper',return_value={'id':12,'article_number':'123'}):
            self.assertEqual(self.client.get('/api/serdes/papers/123/block-diagram?measurement_id=bad').status_code,400)
            with patch.object(app_module,'engine') as engine:
                engine.connect.return_value.__enter__.return_value.execute.return_value.mappings.return_value.first.return_value=None
                self.assertEqual(self.client.get('/api/serdes/papers/123/block-diagram?measurement_id=2180').status_code,404)


if __name__=='__main__':
    unittest.main()
