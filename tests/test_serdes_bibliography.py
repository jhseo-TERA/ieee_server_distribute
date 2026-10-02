import unittest
from unittest.mock import Mock, patch
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.restore_serdes_bibliography import RateLimited, acquire, apply_record, document_ids, get_json, match_record, parse_record
from web.app import _with_bibliography_metadata


class SerdesBibliographyTests(unittest.TestCase):
    def setUp(self):
        self.paper = {'id': 1, 'article_number': '12345678', 'title': 'A PAM4 Receiver', 'doi': '10.1109/example.123'}
        self.record = {'DOI': '10.1109/example.123', 'title': ['A PAM4 Receiver'],
                       'resource': {'primary': {'URL': 'https://ieeexplore.ieee.org/document/12345678/'}},
                       'author': [{'given': 'Alice', 'family': 'Kim'}, {'name': 'Research Group'}],
                       'is-referenced-by-count': 0}

    def test_exact_document_identity_and_zero_count(self):
        value = parse_record(self.paper, self.record)
        self.assertEqual(value['identity_method'], 'exact_ieee_document')
        self.assertEqual(value['citation_count'], 0)
        self.assertEqual(value['authors'], 'Alice Kim; Research Group')

    def test_wrong_document_or_doi_is_rejected(self):
        self.assertIsNone(match_record(dict(self.paper, article_number='99999999'), self.record))
        self.assertIsNone(match_record(dict(self.paper, doi='10.1109/other'), self.record))
        self.assertIsNone(match_record(self.paper, dict(self.record, DOI='10.5555/other')))

    def test_title_search_requires_exact_ieee_id(self):
        paper = dict(self.paper, doi=None)
        self.assertEqual(match_record(paper, self.record), 'exact_ieee_document')
        self.assertIsNone(match_record(paper, dict(self.record, resource={})))

    def test_joint_ieee_proceedings_prefix_still_requires_exact_identity(self):
        record = dict(self.record, DOI='10.23919/example.123', type='proceedings-article')
        self.assertEqual(match_record(dict(self.paper, doi=None), record), 'exact_ieee_document')
        self.assertIsNone(match_record(dict(self.paper, doi=None, article_number='99999999'), record))

    def test_supplement_or_video_is_not_a_paper(self):
        paper = dict(self.paper, doi=None)
        self.assertIsNone(match_record(paper, dict(self.record, type='component')))
        self.assertIsNone(match_record(paper, dict(self.record, DOI='10.1109/example.123/mm1')))

    def test_exact_doi_fallback_requires_matching_title_and_no_conflicting_id(self):
        self.assertEqual(match_record(self.paper, dict(self.record, resource={})), 'exact_doi_title')
        self.assertIsNone(match_record(self.paper, dict(self.record, resource={}, title=['An Unrelated Publication'])))

    def test_non_ieee_urls_do_not_prove_identity(self):
        record = dict(self.record, resource={'primary': {'URL': 'https://ieee.org.evil.example/document/12345678/'}})
        self.assertEqual(document_ids(record), set())
        self.assertIsNone(match_record(dict(self.paper, doi=None), record))

    def test_pdf_arnumber_can_supply_exact_identity(self):
        record = {'link': [{'URL': 'http://xplorestaging.ieee.org/ielx8/12/34/12345678.pdf?arnumber=12345678'}]}
        self.assertEqual(document_ids(record), {'12345678'})

    def test_unknown_and_invalid_counts_stay_null(self):
        for value in (None, '', '12', -1, True, 3.5):
            with self.subTest(value=value):
                self.assertIsNone(parse_record(self.paper, dict(self.record, **{'is-referenced-by-count': value}))['citation_count'])

    def connection(self, current):
        conn = Mock()
        conn.execute.return_value.mappings.return_value.one.return_value = current
        return conn

    def test_apply_fills_missing_values_but_never_favorite_or_measurements(self):
        conn = self.connection({'authors': None, 'doi': None, 'citation_count': None, 'is_favorite': 1})
        value = apply_record(conn, self.paper, parse_record(self.paper, self.record), '2026-09-07T01:00:00+00:00', self.record)
        self.assertEqual(value['fields'], ['authors', 'citation_count', 'citation_source', 'citation_updated_at', 'doi'])
        sql = str(conn.execute.call_args_list[-1].args[0])
        self.assertNotIn('is_favorite=', sql)
        self.assertNotIn('serdes_measurements', sql)

    def test_apply_preserves_existing_fields(self):
        current = {'authors': 'Existing Author', 'doi': self.paper['doi'], 'citation_count': 77, 'is_favorite': 1}
        conn = self.connection(current)
        value = apply_record(conn, self.paper, parse_record(self.paper, self.record), '2026-09-07T01:00:00+00:00', self.record)
        self.assertEqual(value['fields'], [])
        self.assertEqual(conn.execute.call_count, 2)

    def test_nonfavorite_uses_survey_snapshot_not_repo_counter(self):
        conn = self.connection({'authors': 'Existing', 'doi': self.paper['doi'], 'citation_count': None, 'is_favorite': 0})
        value = apply_record(conn, self.paper, parse_record(self.paper, self.record), '2026-09-07T01:00:00+00:00', self.record)
        self.assertEqual(value['fields'], [])
        self.assertIn('serdes_paper_bibliography', str(conn.execute.call_args_list[1].args[0]))

    def test_changed_identity_is_not_written(self):
        conn = self.connection({'authors': None, 'doi': '10.1109/different', 'citation_count': None, 'is_favorite': 1})
        value = apply_record(conn, self.paper, parse_record(self.paper, self.record), '2026-09-07T01:00:00+00:00', self.record)
        self.assertEqual(value['status'], 'identity_changed')
        self.assertEqual(conn.execute.call_count, 1)

    def test_api_fallback_keeps_provenance_and_real_zero(self):
        row = _with_bibliography_metadata({'authors': None, 'citation_count': None, 'bibliography_authors': 'Alice',
            'bibliography_citation_count': 0, 'bibliography_provider': 'crossref', 'bibliography_fetched_at': 'date'})
        self.assertEqual((row['authors'], row['citation_count'], row['citation_source']), ('Alice', 0, 'crossref'))
        self.assertFalse(any(k.startswith('bibliography_') for k in row))
        row = _with_bibliography_metadata({'authors': 'Existing', 'citation_count': 0, 'citation_source': 'ieee',
            'bibliography_citation_count': 42, 'bibliography_provider': 'crossref'})
        self.assertEqual((row['authors'], row['citation_count'], row['citation_source']), ('Existing', 0, 'ieee'))

    def test_cache_is_reused_without_network_or_overwrite(self):
        with TemporaryDirectory() as temp, patch('scripts.restore_serdes_bibliography.CACHE', Path(temp)), patch('scripts.restore_serdes_bibliography.get_json') as http:
            cache = Path(temp) / (self.paper['article_number'] + '.json')
            raw = json.dumps({'status': 'matched', 'record': self.record})
            cache.write_text(raw, encoding='utf-8')
            self.assertEqual(acquire(self.paper, True)[1]['record'], self.record)
            http.assert_not_called()
            self.assertEqual(cache.read_text(encoding='utf-8'), raw)

    def test_no_fetch_does_not_call_provider(self):
        with TemporaryDirectory() as temp, patch('scripts.restore_serdes_bibliography.CACHE', Path(temp)), patch('scripts.restore_serdes_bibliography.get_json') as http:
            self.assertEqual(acquire(self.paper, False)[1]['status'], 'not_cached')
            http.assert_not_called()

    def test_rate_limit_stops_without_retrying(self):
        session = Mock()
        session.get.return_value.status_code = 429
        with patch('scripts.restore_serdes_bibliography.LOCAL', session=session), patch('scripts.restore_serdes_bibliography.NEXT_REQUEST', 0):
            with self.assertRaises(RateLimited):
                get_json('https://api.crossref.org/works/example')
        self.assertEqual(session.get.call_count, 1)

    def test_missing_provider_record_is_not_a_zero_count(self):
        session = Mock()
        session.get.return_value.status_code = 404
        with patch('scripts.restore_serdes_bibliography.LOCAL', session=session), patch('scripts.restore_serdes_bibliography.NEXT_REQUEST', 0):
            self.assertIsNone(get_json('https://api.crossref.org/works/example'))


if __name__ == '__main__':
    unittest.main()
