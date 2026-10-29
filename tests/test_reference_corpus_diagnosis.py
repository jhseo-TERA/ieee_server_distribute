"""A corpus reviewer must not turn model opinions into measured accuracy."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import diagnose_reference_corpus as corpus


class CorpusDiagnosisTests(unittest.TestCase):
    def test_inventory_deduplicates_and_prefers_registered_document(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);out=root/'out'
            for name,content in (('ieee-pdf/a.pdf',b'same'),('optica-pdf/b.pdf',b'same'),
                                 ('drive-zip-extracted/c.pdf',b'unique'),('DesignCon/SLIDES_demo.pdf',b'slides')):
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(content)
            corpus.save(root/'config/reference_benchmark.json',{'papers':[]})
            corpus.save(out/'catalog.json',[{'source_system':'optica','article_number':'b','pdf_local_path':'optica-pdf/b.pdf','year':'2026'}])
            with patch.object(corpus,'ROOT',root):value=corpus.inventory(out)
            self.assertEqual((value['files'],value['selected'],value['duplicates']),(4,2,1))
            self.assertEqual(value['inventory_unique_pdfs'],3)
            self.assertEqual(value['excluded_slide_decks'],1)
            paper=next(p for p in value['papers'] if p['sha256']==hashlib.sha256(b'same').hexdigest())
            self.assertEqual(paper['pdf'],'optica-pdf/b.pdf')
            self.assertEqual(paper['year_band'],'2025_or_newer')
            incoming=next(p for p in value['papers'] if p['pdf'].startswith('drive-zip'))
            self.assertFalse(incoming['supported_library'])
            self.assertNotIn('slides',value['document_hints'])

    def test_reviewer_cannot_omit_or_move_reference_identities(self):
        with self.assertRaises(ValueError):
            corpus.validate_reviews({'reviews':[{'number':2,'verdict':'no_issue_seen','issues':[]}]},[{'number':1,'raw':'First source'}])

    def test_source_consistent_claim_is_logged_without_counting_it_as_an_error(self):
        source='A. Lee, B. Müller, “A real title,” JSSC, 2020.'
        inputs=[{'number':1,'raw':source,'independent_source':source,'candidate':{'author':'Lee'}}]
        claim={'field':'author','kind':'damaged_text','evidence':'B. Müller','reason':'A secondary author has an accent.'}
        row=corpus.validate_reviews({'reviews':[{'number':1,'verdict':'needs_review','issues':[claim]}]},inputs)[0]
        self.assertEqual(row['verdict'],'no_issue_seen')
        self.assertEqual(row['model_verdict'],'needs_review')
        self.assertEqual(row['issues'],[])
        self.assertEqual(row['dismissed_source_consistent_issues'][0]['evidence'],'B. Müller')

    def test_model_outage_waits_but_changed_digest_stops(self):
        from unittest.mock import Mock
        client=Mock()
        client.status.side_effect=[{'available':False},{'available':True,'models':[{'name':corpus.bulk.MODEL,'digest':'same'}]}]
        with tempfile.TemporaryDirectory() as folder,patch.object(corpus.time,'sleep') as sleep:
            corpus.wait_for_model(Path(folder),client,'same')
            sleep.assert_called_once_with(15)
        client.status.side_effect=None
        client.status.return_value={'available':True,'models':[{'name':corpus.bulk.MODEL,'digest':'different'}]}
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):corpus.wait_for_model(Path(folder),client,'same')

    def test_evidence_from_another_reference_is_rejected(self):
        inputs=[{'number':1,'raw':'First source'},{'number':2,'raw':'Other source'}]
        answer={'reviews':[{'number':1,'verdict':'needs_review','issues':[{'field':'title','kind':'wrong_role','evidence':'Other source','reason':'x'}]},
                           {'number':2,'verdict':'no_issue_seen','issues':[]}]}
        rows=corpus.validate_reviews(answer,inputs)
        self.assertEqual(rows[0]['verdict'],'unverifiable')
        self.assertEqual(len(rows[0]['rejected_ungrounded_issues']),1)

    def test_page_excerpt_can_support_boundary_but_not_another_authors_name(self):
        inputs=[{'number':1,'raw':'First source','coverage_context':'[9] Other Author, paper, 2020.'}]
        issue={'field':'boundary','kind':'boundary_error','evidence':'Other Author','reason':'x'}
        answer={'reviews':[{'number':1,'verdict':'needs_review','issues':[issue]}]}
        self.assertEqual(corpus.validate_reviews(answer,inputs)[0]['verdict'],'needs_review')
        issue['field']='author'
        self.assertEqual(corpus.validate_reviews(answer,inputs)[0]['verdict'],'unverifiable')

    def test_document_only_diagnosis_is_not_counted_as_a_reference_review(self):
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder)
            corpus.save(out/'manifest.json',{'papers':[{'id':'one','pdf':'one.pdf','cohort':'new_to_pipeline',
                'year_band':'unknown','document_hint':'slides','year':None}], 'files':1,'duplicates':0})
            corpus.save(out/'papers/one/diagnosis.json',{'document_only':True,'pipeline_status':'extraction_unresolved',
                'reviews':[{'number':0,'verdict':'unverifiable','issues':[]}]})
            value=corpus.report(out)
            self.assertEqual(value['counts']['document_unverifiable'],1)
            self.assertEqual(value['counts']['unverifiable'],0)
            self.assertNotIn('error_rate',value)
            self.assertEqual(value['status'],'running')

    def test_coverage_uses_unsegmented_page_and_authenticates_source_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'evidence.json'
            corpus.save(path,{'source_sha256':'pdf','pages':[{'page':1,'text':'References\n[1] A. Lee, 2020.\nFig. 2. Plot\n[2] B. Wu, "Later," 2021.'}]})
            prep={'pdf_evidence':{'path':str(path),'sha256':corpus.digest(path)}}
            context=corpus.coverage_context(prep,{1},'pdf')
            self.assertIn('Possible unmatched label 2',context)
            with self.assertRaises(ValueError):corpus.coverage_context(prep,{1},'different pdf')


if __name__=='__main__':
    unittest.main()
