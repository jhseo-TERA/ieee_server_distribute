"""Source-layout repairs must preserve literal identifiers and ambiguity."""
import unittest
from paper_server.parsers.reference_identifiers import literal_doi
from paper_server.parsers.reference_roles import publication_year,quoted_title,apply_roles,personal_name
from paper_server.parsers.reference_ocr import ocr_page_plan
from scripts.reference_evidence_worker import geometric_diacritics,geometric_text
from paper_server.parsers.reference_scope import citation_scope
from paper_server.parsers.reference_claims import source_consistency


class SourceAdjudicationTests(unittest.TestCase):
    def test_doi_continuations_retain_literal_hyphen_and_numeric_suffix(self):
        examples={
          'doi: 10.1002/j.1538-\n7305.1960.tb03959.x.':'10.1002/j.1538-7305.1960.tb03959.x',
          'DOI: 10.1109/JSSC.2014. 2360917.':'10.1109/jssc.2014.2360917',
          '10.1109 /JLT.2019.\n2956779.':'10.1109/jlt.2019.2956779',
          '10.23919/VLSITechnologyandCir\n57934.2023.10185362.':'10.23919/vlsitechnologyandcir57934.2023.10185362',
          '10.1109/JSSC.\n2014.2360917.':'10.1109/jssc.2014.2360917',
          '10.1109/MSSC.2016\n.2577978.':'10.1109/mssc.2016.2577978',
          '10.1038/s41928-023-\n01048-1.':'10.1038/s41928-023-01048-1'}
        for source,expected in examples.items():
            with self.subTest(source=source):self.assertEqual(literal_doi(source)['doi'],expected)

    def test_doi_cannot_swallow_later_year_or_page_number(self):
        self.assertEqual(literal_doi('10.1234/finished.\n2024 pp. 1234-1235')['doi'],'10.1234/finished')
        self.assertEqual(literal_doi('10.1109/JSSC.2024.\n[2] A. Lee, 2023.')['doi'],'10.1109/jssc.2024')
        self.assertTrue(literal_doi('10.1109/JSSC.2024.')['incomplete'])
        self.assertIsNone(literal_doi('doi: 10.23919'))

    def test_arxiv_identifier_and_access_bracket_are_not_publication_years(self):
        self.assertEqual(publication_year('A. Lee, "Routing," arXiv:1803.08475 (2018).','Routing')[0],2018)
        self.assertEqual(publication_year('B. Wu, "Report," 25 April 2015. [Accessed 13 12 2022].','Report')[0],2015)
        self.assertEqual(publication_year('A. Lee, "Skew," DesignCon2022.','Skew')[0],2022)
        self.assertIsNone(publication_year('Fisher, Ronald A. (1971) [1935]. The Design of Experiments.','The Design of Experiments')[0])

    def test_host_license_year_and_article_doi_are_not_citation_fields(self):
        text='A. Lee, “A paper,” Jpn. J. Appl. Phys. 58 SBBE02 Authorized licensed use limited to a university. Downloaded on July 26,2026.'
        self.assertIsNone(publication_year(text,'A paper')[0])
        text='Roth, J. E., A modulator, 2008. Nature Photonics | VOL 4 | AUGUST 2010 doi:10.1038/nphoton.2010.179'
        self.assertEqual(publication_year(text,'A modulator')[0],2008)
        self.assertIsNone(literal_doi(text))
        self.assertIsNone(citation_scope('A. Lee, “Figure 1: A circuit,” JSSC, 2020.')[1])
        raw='“A product brief,” Semtech, SMI4039. Delong Cui received B.S. and M.S. degree in physics in 1988.'
        self.assertEqual(citation_scope(raw)[0],'“A product brief,” Semtech, SMI4039.')
        row=apply_roles([{'raw':raw,'number':1,'page':1}])[0]
        self.assertEqual(row['raw'],raw)
        self.assertNotIn('Delong Cui',row['citation_text'])
        self.assertEqual(row['source_view']['raw'],raw)
        self.assertIsNone(row.get('year'))

    def test_detached_diaeresis_requires_geometric_alignment(self):
        text='Sackinger, ¨'
        boxes=[(i*10,0,i*10+5,7) for i in range(len(text))]
        boxes[1]=(10,0,15,5);boxes[-1]=(11,6,14,7)
        fixed,edits=geometric_diacritics(text,lambda i:boxes[i])
        self.assertEqual(fixed,'Säckinger, ')
        self.assertEqual(len(edits),1)
        boxes[-1]=(200,6,204,7)
        self.assertEqual(geometric_diacritics(text,lambda i:boxes[i]),(text,[]))

    def test_nested_title_quotes_do_not_truncate_the_article(self):
        raw='A. Lee, “A receiver enabled by “phase reset”,” IEEE JSSC, 2020.'
        quote=quoted_title(raw)
        self.assertEqual(quote[1],'A receiver enabled by “phase reset”,')
        self.assertEqual(publication_year(raw,None)[0],2020)
        self.assertIsNone(quoted_title('A. Lee, “A receiver with “unclosed title,” IEEE JSSC, 2020.'))

    def test_scientific_uncertainty_routes_only_the_reference_page_to_ocr(self):
        data={'pages':[{'page':1,'text':'Fig. 1. Body caption /Omega and /22m '+('normal body '*20)},
            {'page':2,'text':'References\n[1] A. Lee, “A 0.13 /22m receiver,” 2020.'}]}
        self.assertEqual(ocr_page_plan(data)['pages'],[2])

    def test_geometry_edits_use_original_offsets_when_both_are_needed(self):
        text='S a¨r 231-1'
        boxes=[(i*10,0,i*10+5,7) for i in range(len(text))]
        boxes[0]=(0,0,5,7);boxes[1]=(5,0,6,0);boxes[2]=(6,0,11,5);boxes[3]=(7,6,10,8)
        for i in (7,8):boxes[i]=(i*10,3,i*10+3,7)
        fixed,powers,accents=geometric_text(text,lambda i:boxes[i])
        self.assertEqual(fixed,'Sär 2^31-1')
        self.assertEqual((len(powers),len(accents)),(1,1))

    def test_identifier_line_hyphen_ambiguity_is_retained_for_review(self):
        raw='A. Lee, “A title,” 2024, doi:10.1109/ICCE\ufffe\nASIA63397.2024.10774058.'
        self.assertTrue(literal_doi(raw)['ambiguous_discretionary_hyphen'])
        row=apply_roles([{'raw':raw,'number':1,'page':1}])[0]
        self.assertIn('doi_discretionary_hyphen_requires_review',row['warnings'])

    def test_personal_name_punctuation_and_standard_roles(self):
        self.assertEqual(personal_name('F. Zare., “A title,” 2009.')['family'],'Zare')
        self.assertEqual(personal_name('D. P. Kingma. et al., “A title,” 2017.')['family'],'Kingma')
        self.assertIn('organization',personal_name('Broadcom Ltd., “A datasheet,” 2014.'))
        self.assertIn('organization',personal_name('Graphics Double Data Rate (GDDR5X) SGRAM, JESD232A, 2016.'))
        self.assertEqual(personal_name('A. Lee, “Evaluation of JESD232A,” 2016.')['family'],'Lee')
        self.assertEqual(personal_name('Heni, W. et al. An unquoted title. 2020.')['family'],'Heni')
        for raw in ('T. Beukema et la., “A title,” 2005.','J. Im el al., “A title,” 2020.','C.J., Charles, and X. Zhou. “A title,” 2024.','A. Brown XX YY., “A title,” 2020.'):
            self.assertNotIn('family',personal_name(raw))

    def test_forum_title_list_commas_are_not_author_delimiters(self):
        raw='Optical Internetworking Forum (OIF) Common Electrical I/O - agreements for 6G+ bps, 11G+ bps, 25G+ bps, IA # OIF-CEI-05.1, December 29, 2022.'
        row=apply_roles([{'raw':raw,'number':1,'page':1,'title':'11G+ bps, 25G+ bps, IA # OIF-CEI-05.1, December 29, 2022'}])[0]
        self.assertEqual(row['title'],'Common Electrical I/O - agreements for 6G+ bps, 11G+ bps, 25G+ bps')
        self.assertEqual(row['year'],2022)

    def test_host_headers_and_url_path_years_do_not_create_publication_years(self):
        self.assertEqual(publication_year('A. Lee, “A title,” Opt. Lett. 43, 1554–1557 (2018). Research Article Vol. 13, No. 1 / January 2025 / Photonics Research 193',None)[0],2018)
        self.assertEqual(publication_year('A. Lee, “A title,” JSSC 2017. 98 VOLUME 5, 2025',None)[0],2017)
        self.assertEqual(publication_year('Frlan, E. (Sep. 28, 2015). A report. Accessed: May 10, 2020. [Online]. Available: https://example.org/ 2019/01/report.pdf',None)[0],2015)
        self.assertEqual(publication_year('Li, J., A synthesizer. Nat. Commun. 4, 2097 (2013).',None)[0],2013)
        self.assertIsNone(publication_year('A report, retrieved 11/6/2019',None)[0])

    def test_claim_guard_preserves_mismatches_damaged_text_and_boundary_flags(self):
        source='A. Lee, B. Müller, “A useful title,” JSSC, 2020.'
        original={'independent_source':source,'candidate':{'author':'Lee','title':'A useful title','year':2020}}
        self.assertIsNotNone(source_consistency({'field':'author'},original))
        self.assertIsNotNone(source_consistency({'field':'title'},original))
        self.assertIsNone(source_consistency({'field':'boundary'},original))
        self.assertIsNone(source_consistency({'field':'title'},{**original,'candidate':{'title':'Different title'}}))
        self.assertIsNone(source_consistency({'field':'title'},{'independent_source':'A. Lee, “A 0.13 m CMOS receiver,” 2020.','candidate':{'title':'A 0.13 m CMOS receiver'}}))

    def test_independent_source_cannot_silently_extend_a_readable_family_name(self):
        row={'number':1,'page':1,'raw':'A. Brown, “A useful title,” 2020.','author':'Brown','year':2020,'title':'A useful title'}
        source={1:{**row,'raw':'A. Brown XX YY, “A useful title,” 2020.'}}
        result=apply_roles([row],source)[0]
        self.assertEqual(result['author'],'Brown')
        self.assertIn('independent_author_source_disagreement',result['warnings'])
        # A parser that shortened an explicitly printed compound family can
        # still be corrected when both original and independent sources agree.
        original={**row,'raw':'A. Brown Rose, “A useful title,” 2020.'}
        self.assertEqual(apply_roles([original],{1:original})[0]['author'],'Brown Rose')


if __name__=='__main__':unittest.main()
