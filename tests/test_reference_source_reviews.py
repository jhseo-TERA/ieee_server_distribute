from copy import deepcopy
import hashlib
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from paper_server.parsers.reference_source_reviews import apply_source_reviews,verified_source_review,display_name
from paper_server.parsers.reference_pipeline import reconcile_rows
from paper_server.parsers.reference_authority import apply_author_resolution,load_authorities,verified_author_resolution
from paper_server.parsers.reference_display import micro_unit_display, apply_display_formatting
from scripts.replay_reference_pipeline import evaluation_field_key
from paper_server.parsers import reference_source_reviews as source_reviews


class SourceReviewTests(unittest.TestCase):
    def test_micro_units_use_u_in_display_and_preserve_extracted_fields(self):
        title='A 0.13-μm receiver using 5 µA and 20 μW with 2 μs pulses'
        raw=f'A. Lee, “{title},” 2020.'
        row={'raw':raw,'number':1,'page':1,'author':'Lee','year':2020,'title':title}
        result=reconcile_rows([row])[0]
        self.assertEqual(result['title_display'],'A 0.13-um receiver using 5 uA and 20 uW with 2 us pulses')
        self.assertEqual(result['title_match_key'],'a 0.13-um receiver using 5 ua and 20 uw with 2 us pulses')
        self.assertEqual(result['title'],title)
        self.assertEqual(result['raw'],raw)
        self.assertEqual(apply_display_formatting(result),result)
        self.assertEqual(result['normalization_steps'][-1]['rule'],'micro_unit_ascii')

    def test_greek_variable_and_unknown_or_missing_units_are_not_guessed(self):
        for title in ('A model with μ = 2 and μ_eff', 'A 0.13- m receiver', 'μunknown behavior', None):
            with self.subTest(title=title):
                self.assertEqual(micro_unit_display(title),title)
        unresolved={'title_display':'Ambiguous μm title','title_match_key':None,'normalization_status':'needs_review'}
        result=apply_display_formatting(unresolved)
        self.assertIsNone(result['title_match_key'])
        self.assertEqual(result['normalization_status'],'needs_review')

    def test_display_comparison_accepts_micro_typography_but_keeps_unit_loss_an_error(self):
        self.assertEqual(evaluation_field_key('0.13-μm CMOS','title'),evaluation_field_key('0.13-um CMOS','title'))
        self.assertEqual(evaluation_field_key('5 µA','title'),evaluation_field_key('5 uA','title'))
        self.assertNotEqual(evaluation_field_key('0.13- m CMOS','title'),evaluation_field_key('0.13-um CMOS','title'))
        self.assertNotEqual(evaluation_field_key('μ = 2','title'),evaluation_field_key('u = 2','title'))

    def test_comparison_keeps_inclusive_and_much_smaller_operators(self):
        for title,missing in (('≥40GHz','40GHz'),('≤1 Tbps','1 Tbps'),('IM3 ≪ -60 dBc','IM3 -60 dBc')):
            with self.subTest(title=title):
                self.assertNotEqual(evaluation_field_key(title,'title'),evaluation_field_key(missing,'title'))
        self.assertEqual(evaluation_field_key('≥40GHz','title'),evaluation_field_key('>=40GHz','title'))

    def fixture(self):
        row={'raw':'A. Lee, “A 231-1 PRBS generator,” 2020.','number':6,'page':4,'year':2020,
            'author':'Lee','title':'A 231-1 PRBS generator','title_display':'A 231-1 PRBS generator',
            'warnings':['possible_lost_scientific_glyph'],'normalization_steps':[]}
        record={'id':'test-review','pdf_sha256':'f'*64,'raw_sha256':hashlib.sha256(row['raw'].encode()).hexdigest(),
            'page':4,'number':6,'year':2020,'field':'title_display','before':row['title_display'],
            'after':'A 2^31-1 PRBS generator','evidence':{'kind':'source_pdf_image_review'},
            'resolved_warnings':['possible_lost_scientific_glyph']}
        return row,record

    def test_real_y_diaeresis_uses_plain_y_only_in_display(self):
        self.assertEqual(display_name('Laÿ and Ÿ'),'Lay and Y')
        row={'raw':'A. Laÿ, “Some title,” 2020.','number':1,'page':1,'author':'Laÿ','year':2020,'title':'Some title'}
        result=reconcile_rows([row])[0]
        self.assertEqual(result['author'],'Laÿ')
        self.assertEqual(result['author_display'],'Lay')
        self.assertEqual(result['raw'],row['raw'])

    def test_review_repairs_display_and_preserves_source_and_original_title(self):
        row,record=self.fixture();original=deepcopy(row)
        result=apply_source_reviews(row,'f'*64,[record])
        self.assertEqual(result['title_display'],'A 2^31-1 PRBS generator')
        self.assertEqual(result['title_match_key'],'a 2^31-1 prbs generator')
        self.assertEqual(result['raw'],row['raw']);self.assertEqual(result['title'],row['title'])
        self.assertEqual(row,original)
        self.assertTrue(verified_source_review(result,'f'*64,[record]))
        self.assertEqual(apply_source_reviews(result,'f'*64,[record]),result)

    def test_mismatched_pdf_page_number_year_or_raw_cannot_use_a_review(self):
        row,record=self.fixture()
        for change in ({'page':5},{'number':7},{'year':2021},{'raw':row['raw']+' changed'}, {'title_display':'Other'}):
            with self.subTest(change=change):
                changed={**row,**change}
                self.assertIs(apply_source_reviews(changed,'f'*64,[record]),changed)
        self.assertIs(apply_source_reviews(row,'e'*64,[record]),row)
        self.assertIs(apply_source_reviews(row,'f'*64,[record,record]),row)

    def test_forged_or_changed_review_evidence_is_rejected(self):
        row,record=self.fixture();result=apply_source_reviews(row,'f'*64,[record])
        for change in ({'record_sha256':'0'*64},{'after':'A 2^30-1 PRBS generator'}):
            tampered={**result,'source_review_resolution':{**result['source_review_resolution'],**change}}
            self.assertFalse(verified_source_review(tampered,'f'*64,[record]))
        self.assertFalse(verified_source_review(result,'f'*64,[{**record,'reviewed_on':'2099-01-01'}]))

    def test_reviewed_glyph_survives_ocr_variant_only_at_the_same_source_context(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);image=root/'source.png';image.write_bytes(b'authenticated source-image fixture')
            title='A fully integrated 0.13- m CMOS 40-Gb/s serial link transceiver'
            raw=f'A. Lee, “{title},” JSSC, 2009.'
            record={'id':'review','pdf_sha256':'f'*64,'raw_sha256':hashlib.sha256(raw.encode()).hexdigest(),
                'page':14,'number':19,'year':2009,'field':'title_display','before':title,'after':title.replace('- m','-um'),
                'resolved_warnings':[],'evidence':{'kind':'source_pdf_image_review','image':'source.png',
                    'image_sha256':hashlib.sha256(image.read_bytes()).hexdigest()}}
            current=title.replace('- m','-j:m')
            row={'raw':raw.replace(title,current),'title_display':current,'number':19,'page':14,'year':2009,
                 'ocr_required_review':True,'warnings':[]}
            with patch.object(source_reviews,'ROOT',root):
                result=apply_source_reviews(row,'f'*64,[record])
                self.assertEqual(result['title_display'],record['after'])
                self.assertEqual(result['raw'],row['raw'])
                self.assertTrue(verified_source_review(result,'f'*64,[record]))
                self.assertEqual(apply_source_reviews(result,'f'*64,[record]),result)
                for change in ({'page':13},{'number':18},{'year':2010},{'ocr_required_review':False},
                    {'raw':row['raw'].replace('0.13','0.18'),'title_display':current.replace('0.13','0.18')},
                    {'title_display':'A different receiver'}):
                    modified={**row,**change}
                    self.assertIs(apply_source_reviews(modified,'f'*64,[record]),modified)
                image.write_bytes(b'changed image')
                self.assertIs(apply_source_reviews(row,'f'*64,[record]),row)

    def test_ntu_indexed_name_resolves_family_and_keeps_complete_first_author(self):
        raw='Jri Lee, M.-S. Chen, and H.-D. Wang, “A 20Gb/s Duobinary Transceiver in 90nm CMOS,” ISSCC Dig. Tech. Papers, pp. 102-103, 2008.'
        row={'raw':raw,'number':3,'page':1,'year':2008,'author':'Jri','title':'A 20Gb/s Duobinary Transceiver in 90nm CMOS','doi':None}
        result=reconcile_rows([row])[0]
        self.assertEqual(result['author'],'Lee')
        self.assertEqual(result['first_author_name'],'Jri Lee')
        self.assertEqual(result['first_author_display'],'Jri Lee')
        self.assertEqual(result['raw'],raw)
        self.assertTrue(verified_author_resolution(result,load_authorities()))
        self.assertFalse(result['needs_review'])
        wrong={**row,'title':'A different paper'}
        self.assertIs(apply_author_resolution(wrong,load_authorities()),wrong)


if __name__=='__main__':unittest.main()
