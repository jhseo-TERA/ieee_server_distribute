"""Boundary and role evidence must beat agreement without guessing absent data."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from paper_server.parsers.reference_segmentation import segment_references, choose_entries, entry_quality
from paper_server.parsers.reference_roles import apply_roles, personal_name, publication_year
from paper_server.parsers.reference_evidence import align_sources
from paper_server.parsers.reference_evidence import authenticated_sources, bounded_evidence
from scripts.reference_evidence_worker import geometric_exponents
from paper_server.parsers.reference_pipeline import reconcile_rows


def row(raw, **fields):
    return {'number':1,'page':1,'raw':raw,'author':None,'title':None,'year':None,'doi':None,**fields}


class ReferenceBoundaryV2Tests(unittest.TestCase):
    def test_malformed_ocr_label_requires_layout_without_renumbering(self):
        for label in ('(2]','[2)'):
            text = f'References\n[1] A. Lee, “First,” 2020.\n{label} B. Wu, “Second,” 2021.'
            result = segment_references([(1,text)])
            self.assertEqual(len(result['entries']),1)
            self.assertIn(label,result['entries'][0]['raw'])
            self.assertIn('collapsed_reference_labels',entry_quality(result['entries']))
        self.assertNotIn('collapsed_reference_labels',entry_quality([row('A. Lee, “A (2) pole receiver,” 2020.')]))

    def test_wrapped_biography_verb_still_ends_bibliography(self):
        text = ('References\n[1] A. Lee, “First,” 2020.\n'
                'Full Name (Fellow, IEEE) received\nthe Ph.D. degree in 2002.')
        self.assertNotIn('2002',segment_references([(1,text)])['entries'][0]['raw'])
    def test_running_header_and_author_biography_do_not_join_book_citation(self):
        result = segment_references([(9,'References\n[1] B. Lee, Design of Circuits. New York: Publisher, 2001.'),
            (10,'LEE et al.: A LONG RUNNING HEADER 999\n'
                'Full Name (Member, IEEE) was born on April 1, 1976.\nHe received the degree in 2002.')])
        self.assertEqual(result['entries'][0]['raw'],'B. Lee, Design of Circuits. New York: Publisher, 2001.')
        self.assertNotIn('2002',result['entries'][0]['raw'])

    def test_figure_interruption_requires_layout_but_table_labels_do_not_join(self):
        text = ('References\n[1] A. Lee, “First,” 2020.\nFig. 1. Measured result.\n'
                '[8] [9] This work\n[2] B. Wu, “Second,” 2021.')
        result = segment_references([(1, text)])
        self.assertEqual(len(result['entries']), 1)
        self.assertIn('possible_omitted_references', result['warnings'])
        self.assertNotIn('Measured', result['entries'][0]['raw'])
        no_continuation = segment_references([(1, text.rsplit('\n[2]', 1)[0])])
        self.assertEqual(no_continuation['warnings'], [])

    def test_explicit_additional_references_extend_only_the_next_sequential_run(self):
        pages = [(1, 'References\n[1] A. Lee, “First,” 2020.\nFig. 1. Body.'),
                 (3, 'Additional References:\n[2] B. Wu, “Second,” 2021.\n[3] C. Li, “Third,” 2022.')]
        result = segment_references(pages)
        self.assertEqual([(e['number'],e['page']) for e in result['entries']], [(1,1),(2,3),(3,3)])
        pages[1] = (3, pages[1][1].replace('[2]', '[4]').replace('[3]', '[5]'))
        self.assertEqual(len(segment_references(pages)['entries']), 1)
        self.assertIn('unresolved_additional_references',segment_references(pages)['warnings'])

    def test_dot_style_front_bibliography_stops_before_introduction(self):
        result=segment_references([(1,'REFERENCES AND LINKS\n1. A. Lee, “Optical link,” (2020).\n'
            '2. “Technology handbook,” July 2021.\n1. Introduction\nBody [1] and [2].')])
        self.assertEqual(len(result['entries']),2)
        self.assertNotIn('Introduction',result['entries'][-1]['raw'])

    def test_inline_short_citations_are_separate_and_figures_are_excluded(self):
        result=segment_references([(3,'References:\n[1] A. Lee, IEEE JSSC, 2020. [2] B. Wu, ISSCC, 2021.\n'
            'Figure 20.6.1: Proposed topology.\n[1] table label')])
        self.assertEqual([e['number'] for e in result['entries']],[1,2])
        self.assertNotIn('topology',result['entries'][-1]['raw'])

    def test_footer_doi_is_never_attached_to_last_citation(self):
        result=segment_references([(1,'REFERENCES\n[1] A. Lee, “A real title,” 2020.\n'
            '2024 IEEE Conference | DOI: 10.1234/host\nAuthorized licensed use limited to: X')])
        self.assertNotIn('10.1234',result['entries'][0]['raw'])

    def test_affiliations_and_body_lists_without_heading_are_unresolved(self):
        for text in ('1. Company A\n2. Company B','Body\n[1] A. Lee, 2020.\n[2] B. Wu, 2021.'):
            self.assertEqual(segment_references([(1,text)])['entries'],[])

    def test_noncontiguous_labels_remain_unresolved(self):
        for labels in ((1,3),(1,1)):
            text='REFERENCES\n'+'\n'.join(f'[{n}] A. Lee, “A title,” 2020.' for n in labels)
            self.assertEqual(segment_references([(1,text)])['status'],'extraction_unresolved')

    def test_cross_page_label_uses_digits_not_prior_newlines(self):
        result=segment_references([(4,'REFERENCES\n[1] A. Lee, “A title,” 2020.\n'),
                                   (5,'\n[2] B. Wu, “Other,” 2021.')])
        self.assertEqual([e['page'] for e in result['entries']],[4,5])

    def test_independent_source_can_replace_a_collapsed_primary(self):
        bad=[row('A. Lee, 2020. [2] B. Wu, 2021. [3] C. Li, 2022.')]
        source=segment_references([(1,'References\n[1] A. Lee, 2020. [2] B. Wu, 2021. [3] C. Li, 2022.')])
        chosen,diag=choose_entries(bad,source)
        self.assertEqual(len(chosen),3)
        self.assertIn('collapsed_reference_labels',diag['warnings'])

    def test_reference_number_alone_cannot_align_different_papers(self):
        self.assertEqual(align_sources([row('A. Lee, Completely different, 2020.')],
            {'entries':[row('B. Wu, Totally unrelated, 2022.')]}),{})


class ReferenceRoleV2Tests(unittest.TestCase):
    def test_partial_independent_citation_cannot_delete_explicit_original_year(self):
        original = row('A. Lee, “A receiver,” 2020 IEEE Conference, 2020.',title='A receiver',author='Lee',year=2020)
        source = {1:{**original,'raw':'A. Lee, “A receiver,”'}}
        self.assertEqual(apply_roles([original],source)[0]['year'],2020)

    def test_biography_tail_cannot_override_clean_structured_source(self):
        original = row('A. Lee, “A long and completely real receiver title,” 2020.',title='A long and completely real receiver title',year=2020)
        source = {'entries':[{**original,'raw':original['raw']+' Full Name received the Ph.D. degree in 2002.'}]}
        self.assertEqual(align_sources([original],source),{})
        self.assertEqual(reconcile_rows([original],bounded=source)[0]['year'],2020)

    def test_online_visit_date_is_not_a_publication_date(self):
        for marker in ('visited on','retrieved on','accessed on'):
            value=f'IEEE baseline summary. [Online]. https://example.org/file.pdf ({marker} 03/01/2015).'
            self.assertIsNone(publication_year(value,'IEEE baseline summary')[0])

    def test_wrapped_doi_token_is_rejoined_only_with_literal_line_evidence(self):
        original = row('A. Lee, “A receiver,” 2024, doi: 10.1234/TEST. 2024.12345',title='A receiver')
        source = {1:{**original,'source_raw':'A. Lee, “A receiver,” 2024, doi: 10.1234/TEST.\r\n2024.12345'}}
        self.assertEqual(apply_roles([original],source)[0]['doi'],'10.1234/test.2024.12345')

    def test_complete_doi_cannot_absorb_the_next_page_number(self):
        for page in ('125','0','2024'):
            original=row('A. Lee, “A receiver,” 2018, doi: 10.1234/TEST.2018.12345. '+page,title='A receiver')
            source={1:{**original,'source_raw':'doi: 10.1234/TEST.2018.12345.\r\n'+page}}
            self.assertEqual(apply_roles([original],source)[0]['doi'],'10.1234/test.2018.12345')

    def test_running_journal_header_year_is_not_the_cited_year(self):
        raw=('A. Lee, “A network,” in Proc. Conference, 1998, pp. 100–120. '
             '6298 JOURNAL OF LIGHTWAVE TECHNOLOGY, VOL. 38, NO. 22, NOVEMBER 15, 2020')
        self.assertEqual(publication_year(raw,'A network')[0],1998)

    def test_uncertain_scientific_glyphs_are_flagged_without_guessing_replacement(self):
        for title in ('A receiver with 0.25ȝm technology','A 40-Gb/s Ő8 optical transmitter',
                      'A 4 16 Gb/s receiver','A 2 oversampling transceiver'):
            result=apply_roles([row(f'A. Lee, “{title},” 2020.',title=title)])[0]
            self.assertEqual(result['title'],title)
            self.assertIn('possible_lost_scientific_glyph',result['warnings'])
            self.assertTrue(result['needs_review'])
    def test_standard_title_survives_null_model_and_corporate_suffix_is_not_a_surname(self):
        standard = apply_roles([row('IEEE Standard IEEE802.3ae, 2002.',warnings=['unquoted_or_missing_title'])])[0]
        self.assertEqual(standard['title'],'IEEE Standard IEEE802.3ae')
        self.assertEqual(standard['year'],2002)
        self.assertIsNone(standard['author'])
        self.assertEqual(personal_name('Example Tools, Inc., Software Manual. 2007')['organization'],'Example Tools')

    def test_numeric_line_wrap_is_normalized_only_in_display(self):
        original = row('Cisco Forecast, 2017- 2022.',title='Cisco Forecast, 2017- 2022.')
        result = reconcile_rows([original])[0]
        self.assertEqual(result['title_display'],'Cisco Forecast, 2017-2022.')
        self.assertEqual(result['raw'],original['raw'])
    def test_missing_grounded_field_is_reviewed_but_legitimate_short_citation_is_not(self):
        rejected = row('A. Lee, Conference, 2020.', warnings=['title_not_in_source'])
        result = reconcile_rows([rejected])[0]
        self.assertTrue(result['needs_review'])
        self.assertIn('unresolved_field_grounding', result['warnings'])
        short = row('A. Lee, ISSCC, pp. 1-2, 2020.', warnings=['title_absent_in_short_reference'])
        self.assertFalse(reconcile_rows([short])[0].get('needs_review', False))

    def test_control_glyph_is_visible_and_reviewable_without_changing_raw(self):
        original = row('A. Lee, “A 0.13\x16m receiver,” 2020.', title='A 0.13\x16m receiver')
        result = reconcile_rows([original])[0]
        self.assertEqual(result['raw'], original['raw'])
        self.assertIn('\ufffd', result['title_display'])
        self.assertNotIn('\x16', result['title_display'])
        self.assertIsNone(result['title_match_key'])
        self.assertTrue(result['needs_review'])

    def test_initials_do_not_discard_compound_family_name(self):
        for family in ('De Silva','Van der Meer','Moayedi Pour Fard'):
            self.assertEqual(personal_name(f'M. {family}, A. Other, “Title,” 2020.')['family'],family)

    def test_organization_is_preserved_outside_personal_surname(self):
        original=row('IEEE P802.3zz. Accessed: Nov. 1, 2021. [Online]. Available: https://example.org/202109',
                     author='IEEE',year=2021)
        result=apply_roles([original])[0]
        self.assertIsNone(result['author']); self.assertIsNone(result['year'])
        self.assertEqual(result['title'],'IEEE P802.3zz')
        self.assertTrue(result['author_organization'].startswith('IEEE'))
        self.assertEqual(original['year'],2021)

    def test_title_interval_and_access_date_cannot_be_publication_year(self):
        self.assertEqual(publication_year('Cisco Report (2016-2021), Cisco, White Paper, 2018.',
                                         'Cisco Report (2016-2021)')[0],2018)
        self.assertIsNone(publication_year('Ansys Tool. Accessed: Dec. 2, 2023. https://example.org/2024', 'Ansys Tool')[0])

    def test_page_ranges_cannot_override_explicit_publication_year(self):
        self.assertEqual(publication_year('A. Lee, “Title,” vol.2, pp. 2070–2077, 2014.','Title')[0],2014)
        self.assertEqual(publication_year('A. Lee, “Title,” pp. 1848–1856, 2010, doi: 10.1234/T.2009.2037847.','Title')[0],2010)

    def test_etc_is_not_part_of_a_family_name(self):
        self.assertEqual(personal_name('M. Li etc, Photonics Research, 2018.')['family'],'Li')

    def test_repeat_author_is_local_and_cannot_resolve_first_entry(self):
        rows=[row('A. Silva, “First,” 2020.',author='Wrong'),
              {**row('——, “Second,” 2021.',author='——'),'number':2}]
        original=deepcopy(rows)
        result=apply_roles(rows)
        self.assertEqual([r['author'] for r in result],['Silva','Silva'])
        self.assertEqual(result[1]['author_repeat_of'],1)
        self.assertIsNone(apply_roles([rows[1]])[0]['author'])
        self.assertEqual(rows,original)

    def test_abbreviated_particle_requires_unique_same_bibliography_evidence(self):
        rows=[row('T. De Silva, “First,” 2019.'),{**row('T. D. Silva, “Other,” 2020.'),'number':2}]
        self.assertEqual(apply_roles(rows)[1]['author'],'De Silva')
        self.assertEqual(apply_roles(rows[1:])[0]['author'],'Silva')

    def test_ambiguous_dates_stay_empty_and_reviewable(self):
        result=apply_roles([row('A. Lee, “Title,” 2019, updated 2020.',year=2019)])[0]
        self.assertIsNone(result['year']);self.assertTrue(result['needs_review'])

    def test_discretionary_line_hyphen_is_not_a_doi_character(self):
        original=row('A. Lee, “Title,” 2020. doi: 10.1234/TC-SII.2020.123')
        source={1:{**original,'raw':'A. Lee, “Title,” 2020. doi: 10.1234/TCSII.2020.123',
                   'source_raw':'doi: 10.1234/TC\ufffe\nSII.2020.123'}}
        result=apply_roles([original],source)[0]
        self.assertEqual(result['doi'],'10.1234/tcsii.2020.123')
        self.assertEqual(result['raw'],original['raw'])

    def test_power_notation_requires_actual_raised_glyphs(self):
        text='A 211-1 PRBS'
        normal=lambda i:(i*5,0,i*5+4,10)
        self.assertEqual(geometric_exponents(text,normal),(text,[]))
        def raised(i):
            return (i*5,5,i*5+3,12) if i in (3,4) else normal(i)
        result,edits=geometric_exponents(text,raised)
        self.assertEqual(result,'A 2^11-1 PRBS')
        self.assertEqual(len(edits),1)

    def test_alternate_text_cannot_delete_a_unit_glyph_from_an_existing_title(self):
        original=row('A. Lee, “A receiver in 0.18μm CMOS,” 2020.',title='A receiver in 0.18μm CMOS',author='Lee',year=2020)
        source={1:{**original,'raw':'A. Lee, “A receiver in 0.18m CMOS,” 2020.'}}
        result=apply_roles([original],source)[0]
        self.assertEqual(result['title'],original['title'])
        self.assertIn('independent_title_text_disagreement',result['warnings'])

    def test_suspected_missing_glyphs_are_reviewed_without_fabricating_text(self):
        for title in ('A 231-1 Pseudorandom Binary Sequence Generator','A 0.13- m CMOS receiver'):
            result=apply_roles([row(f'A. Lee, “{title},” 2020.',title=title)])[0]
            self.assertEqual(result['title'],title)
            self.assertIn('possible_lost_scientific_glyph',result['warnings'])
            result=reconcile_rows([{**row(f'A. Lee, “{title},” 2020.',title=title),'needs_review_before_normalization':False}])[0]
            self.assertTrue(result['needs_review'])

    def test_literal_citation_doi_line_is_not_mistaken_for_a_host_footer(self):
        result=segment_references([(1,'References\r\n[1] A. Lee, ”A title,”\r\n'
            '2020 IEEE Conference, 2020, doi: 10.1234/cited.\r\n')])
        self.assertIn('10.1234/cited',result['entries'][0]['raw'])

    def test_wrapped_biography_member_line_ends_bibliography(self):
        result=segment_references([(1,'References\n[1] A. Lee, “A title,” 2020.\n'
            'Full Name (Graduate Student Member,\nIEEE) was born in 1993.')])
        self.assertNotIn('1993',result['entries'][0]['raw'])

    def test_alternate_source_evidence_must_match_file_hash_pdf_and_reference(self):
        original=row('A. Lee, “A 2d-order receiver for high-speed links,” 2020.',
                     title='A 2d-order receiver for high-speed links',author='Lee',year=2020)
        payload={'source_sha256':'f'*64,'extractor':'pypdfium2','pages':[{'page':1,
            'text':'References\n[1] A. Lee, “A 2nd-order receiver for high-speed links,” 2020.'}]}
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'evidence.json';path.write_text(json.dumps(payload),encoding='utf-8')
            evidence={**payload,'evidence_path':str(path),'evidence_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
            result=reconcile_rows([original],bounded=bounded_evidence(evidence),evidence=evidence,source_sha256='f'*64)
            self.assertEqual(result[0]['title_display'],'A 2nd-order receiver for high-speed links')
            self.assertEqual(authenticated_sources(result,'f'*64)[1]['page'],1)
            self.assertEqual(result[0]['raw'],original['raw'])
            with self.assertRaisesRegex(ValueError,'identity'):
                authenticated_sources(result,'e'*64)
            path.write_text(path.read_text(encoding='utf-8')+' ',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'changed'):
                authenticated_sources(result,'f'*64)

    def test_flat_pdf_text_cannot_erase_existing_structured_subscript(self):
        original=row('A. Lee, “An HBT with $f_{T}$ of 320 GHz,” 2020.',title='An HBT with $f_{T}$ of 320 GHz')
        result=reconcile_rows([original],bounded={'entries':[{**original,'raw':'A. Lee, “An HBT with fT of 320 GHz,” 2020.'}]})[0]
        self.assertIn('f_T',result['title_display'])

    def test_unclosed_quote_cannot_swallow_publication_year_inside_an_overlong_title(self):
        raw='A. Lee, “A driver with malformed $G_{m}$ in IEEE A-SSCC, 2015.'
        self.assertEqual(publication_year(raw,raw.split('“')[1])[0],2015)

    def test_verified_geometric_exponent_can_add_semantics_to_flattened_title(self):
        original=row('A. Lee, “A 211-1 PRBS receiver,” 2020.',title='A 211-1 PRBS receiver')
        source={**original,'raw':'A. Lee, “A 2^11-1 PRBS receiver,” 2020.'}
        result=reconcile_rows([original],bounded={'entries':[source]})[0]
        self.assertEqual(result['title_display'],'A 2^11-1 PRBS receiver')


if __name__=='__main__':
    unittest.main()
