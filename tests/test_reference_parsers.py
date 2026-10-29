import unittest

from paper_server.parsers.references import consensus, grounded, mineru_references, parse_tei, split_numbered


class ReferenceParserTests(unittest.TestCase):
    def test_split_keeps_page_and_cross_page_continuation(self):
        rows = split_numbered([(4, 'REFERENCES\n[1] A. Author, "Title of a\n'),
                               (5, 'paper," 2020.\n[2] B. Writer, "Other," 2021.')])
        self.assertEqual([row['page'] for row in rows], [4, 5])
        self.assertIn('Title of a paper', rows[0]['raw'])

    def test_body_brackets_do_not_create_references(self):
        self.assertEqual(split_numbered([(1, 'Body\n[1] not a References section')]), [])

    def test_duplicate_or_missing_numbers_rejected(self):
        for text in ('[1] A\n[1] B', '[1] A\n[3] C'):
            self.assertEqual(split_numbered([(2, 'REFERENCES\n' + text)]), [])

    def test_mineru_excludes_table_and_biography_blocks(self):
        payload = {'pages': [{'page_idx': 3, 'blocks': [
            {'type': 'ref_text', 'content': '[1] A. Author, "Title," 2020.', 'bbox': [0, 0, 1, 1]},
            {'type': 'table', 'content': '[2] invented table citation'},
            {'type': 'text', 'content': 'Biography [3]'}]}]}
        rows = mineru_references(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['page'], 4)
        self.assertEqual(rows[0]['bbox'], [0, 0, 1, 1])

    def test_mineru_preserves_continuation_and_strips_markup(self):
        payload = {'pages': [{'page_idx': 0, 'blocks': [
            {'type': 'ref_text', 'content': '[1] <i>A title</i>, [link](https://example.org)'},
            {'type': 'ref_text', 'content': 'continued, 2020.'}]}]}
        self.assertEqual(mineru_references(payload)[0]['raw'], 'A title, link continued, 2020.')

    def test_mineru_rejects_out_of_order_labels(self):
        with self.assertRaises(ValueError):
            mineru_references({'pages': [{'page_idx': 0, 'blocks': [
                {'type': 'ref_text', 'content': '[2] no preceding reference'}]}]})

    def test_final_text_block_recovered_only_with_bibliographic_geometry(self):
        payload = {'pages': [{'page_idx': 0, 'blocks': [
            {'type': 'text', 'content': '[2] A. Lee et al., JSSC, 2020, pp.1-2', 'bbox': [.1,.22,.4,.24]},
            {'type': 'ref_text', 'content': '[1] A. Lee, "Paper," 2019.', 'bbox': [.1,.2,.4,.22]},
            {'type': 'text', 'content': '[3] IEEE 2021 et al.', 'bbox': [.6,.24,.9,.26]}]}]}
        self.assertEqual(len(mineru_references(payload)), 1)
        rows = mineru_references(payload, recover_adjacent=True)
        self.assertEqual([r['number'] for r in rows], [1, 2])
        self.assertTrue(rows[-1]['recovered_adjacent_text'])

    def test_unsupported_title_year_and_doi_are_removed(self):
        row = grounded({'number': 1, 'page': 2, 'raw': 'A. Lee, "An actual title," 2020.'},
                       {'title': 'An invented title', 'year': 2024, 'doi': '10.1000/invented'}, 'test')
        self.assertIsNone(row['title'])
        self.assertIsNone(row['year'])
        self.assertIsNone(row['doi'])
        self.assertEqual(len(row['warnings']), 3)

    def test_unicode_ligatures_and_doi_prefix_are_normalized(self):
        row = grounded({'number': 1, 'page': 2, 'raw': 'A. Lee, "A ﬁber link," 2020. doi:10.1234/ABC.'},
                       {'title': 'A fiber link', 'year': 2020, 'doi': 'https://doi.org/10.1234/ABC'}, 'test')
        self.assertEqual(row['title'], 'A fiber link')
        self.assertEqual(row['doi'], '10.1234/abc')
        self.assertEqual(row['warnings'], [])

    def test_tei_namespace_and_missing_title(self):
        xml = '<listBibl><biblStruct xmlns="http://www.tei-c.org/ns/1.0"><analytic><author><persName><surname>Lee</surname></persName></author></analytic><monogr><imprint><date when="2020-01"/></imprint></monogr></biblStruct></listBibl>'
        rows = parse_tei(xml, [{'number': 1, 'page': 5, 'raw': 'A. Lee, JSSC, 2020.'}])
        self.assertEqual(rows[0]['author'], 'Lee')
        self.assertEqual(rows[0]['year'], 2020)
        self.assertIsNone(rows[0]['title'])

    def test_tei_count_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            parse_tei('<listBibl/>', [{'number': 1, 'raw': 'x', 'page': 1}])

    def test_consensus_does_not_truncate_explicitly_quoted_numeric_title(self):
        entry = {'number': 1, 'page': 1, 'raw': 'A. Lee, "1.4-8 Gb/s CDR," 2020.'}
        regex = {'title': '1.4-8 Gb/s CDR', 'author': 'Lee', 'year': 2020}
        grobid = {'title': 'Gb/s CDR', 'author': 'Lee', 'year': 2020}
        row = consensus(entry, regex, grobid)
        self.assertEqual(row['title'], '1.4-8 Gb/s CDR')
        self.assertFalse(row['needs_llm'])

    def test_consensus_routes_missing_title_and_validates_model_repair(self):
        entry = {'number': 1, 'page': 1, 'raw': 'A. Lee, ISSCC, 2020.'}
        regex = {'title': None, 'author': 'Lee', 'year': 2020}
        grobid = dict(regex)
        row = consensus(entry, regex, grobid, {'title': 'Imaginary paper title', 'author': 'Lee', 'year': 2020})
        self.assertTrue(row['needs_llm'])
        self.assertIsNone(row['title'])
        self.assertIn('title_not_in_source', row['warnings'])

    def test_report_organization_is_not_a_personal_surname(self):
        entry = {'number': 1, 'page': 1, 'raw': 'Cisco Annual Internet Report (2018-2023), Cisco, 2018.'}
        row = consensus(entry, {'title': None, 'author': 'Report', 'year': 2018},
                        {'title': None, 'author': None, 'year': 2018},
                        {'title': 'Cisco Annual Internet Report (2018-2023)', 'author': 'Cisco', 'year': 2018})
        self.assertEqual(row['title'], 'Cisco Annual Internet Report (2018-2023)')
        self.assertIsNone(row['author'])

    def test_consensus_preserves_prior_model_grounding_rejections(self):
        entry = {'number': 1, 'page': 1, 'raw': 'A. Lee, ISSCC, 2020.'}
        fields = {'title': None, 'author': 'Lee', 'year': 2020}
        row = consensus(entry, fields, fields, {**fields, 'warnings': ['title_not_in_source']})
        self.assertIn('title_not_in_source', row['warnings'])

    def test_agreed_full_name_surname_is_preserved(self):
        entry = {'number': 1, 'page': 2, 'raw': 'Rajesh Inti, et al., VLSI Circuits, pp.346-347, Jun. 2015'}
        fields = {'title': None, 'author': 'Inti', 'year': 2015}
        row = consensus(entry, fields, fields, {'author': 'Rajesh', 'year': 2015, 'title': None})
        self.assertEqual(row['author'], 'Inti')
        self.assertFalse(row['needs_llm'])
        self.assertIsNone(row['title'])

    def test_agreed_author_not_overwritten_during_book_review(self):
        entry = {'number': 1, 'page': 1, 'raw': 'Ian Goodfellow, Deep Learning, MIT Press, 2016.'}
        regex = {'title': None, 'author': 'Goodfellow', 'year': 2016}
        grobid = {**regex, 'title': 'Deep Learning'}
        row = consensus(entry, regex, grobid, {'title': 'Deep Learning', 'author': 'Ian', 'year': 2016})
        self.assertTrue(row['needs_llm'])
        self.assertEqual(row['author'], 'Goodfellow')
        self.assertEqual(row['title'], 'Deep Learning')

    def test_llm_venue_cannot_fill_missing_article_title(self):
        entry = {'number': 1, 'page': 1, 'raw': 'Saxena, S., et al., VLSI Circuits, pp.352-353, Jun. 2015'}
        regex = {'title': None, 'author': 'Saxena', 'year': 2015}
        grobid = {**regex, 'author': None}
        row = consensus(entry, regex, grobid, {'title': 'VLSI Circuits', 'journal': 'VLSI Circuits',
                                              'author': 'Saxena', 'year': 2015})
        self.assertIsNone(row['title'])
        self.assertIn('title_equals_venue_rejected', row['warnings'])


if __name__ == '__main__':
    unittest.main()
