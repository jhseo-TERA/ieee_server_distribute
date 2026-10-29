from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from paper_server.citation_fallback import (
    CitationLibrary, SupplementedCitationProvider, extract_references, matched, reference_entry, sql_work,
)
from paper_server.citation_graph import build_citation_graph, CitationGraphService
from paper_server.citation_http import SourceUnavailable
from paper_server.citation_provider import ProviderError
from tests.test_citation_graph import snapshot, work


DOI = '10.1109/seed'


def local(article,doi,title='A precise optical modulator study',authors='H. Liu',year='2024'):
    return {'article_number':article,'doi':doi,'title':title,'authors':authors,'year':year,
            'source_system':'ieee','source_name':'JSSC','pdf_available':1}


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict('os.environ', {'IEEE_API_KEY':''})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.library, self.primary, self.http = Mock(), Mock(), Mock()
        self.seed_row = local('101',DOI)
        self.library.by_doi.side_effect = lambda doi: self.seed_row if doi==DOI else None
        self.library.match.return_value = None
        self.library.pdf.return_value = None
        self.primary.lookup_dois.return_value = []
        self.http.counts.return_value = {}
        self.http.budget.return_value = {'limit':50,'used':1,'remaining':49}
        self.provider = SupplementedCitationProvider(self.library,self.primary,self.http)
        self.source = snapshot(work(1,doi=DOI))
        self.primary.fetch.side_effect = lambda doi:deepcopy(self.source)

    def test_openalex_references_skip_crossref_and_pdf(self):
        self.source = snapshot(work(1,[2],doi=DOI),work(2))
        result = self.provider.fetch(DOI)
        self.http.get.assert_not_called()
        self.library.pdf.assert_not_called()
        self.assertEqual(result['works'][0]['reference_ids'],['W2'])

    def test_crossref_references_are_verified_and_pdf_is_not_read(self):
        self.http.get.return_value = {'message':{'DOI':DOI,'reference':[{'DOI':'10.1234/ref','article-title':'A precise optical modulator study','author':'Liu','year':'2024'}]}}
        self.library.match.return_value = local('202','10.1234/ref')
        result = self.provider.fetch(DOI)
        self.assertEqual(result['meta']['reference_origin'],'crossref')
        self.assertEqual(result['meta']['reference_fetched'],1)
        self.library.pdf.assert_not_called()
        graph = build_citation_graph(result)
        self.assertEqual(graph['lineage_edges'][0]['reference_source'],'crossref')

    def test_pdf_fallback_keeps_page_and_reference_number_and_original_input(self):
        self.http.get.return_value = {'message':{'DOI':DOI,'reference':[]}}
        self.library.pdf.return_value = Path('a.pdf')
        entry = reference_entry('H. Liu et al., “A precise optical modulator study,” JSSC, 2024.',5,13)
        self.library.match.return_value = local('202','10.1234/ref')
        self.primary.lookup_dois.return_value = [work(2,doi='10.1234/ref')]
        with patch('paper_server.citation_fallback.extract_references',return_value=[entry]):
            result = self.provider.fetch(DOI)
        self.assertEqual(self.source['works'][0]['reference_ids'],[])
        self.assertEqual(result['works'][0]['reference_ids'],['W2'])
        graph = build_citation_graph(result)
        edge = graph['lineage_edges'][0]
        self.assertEqual((edge['source'],edge['target']),('W2','W1'))
        self.assertEqual((edge['reference_source'],edge['reference_number'],edge['reference_page']),('pdf',5,13))
        self.assertEqual(graph['meta']['reference_total'],1)

    def test_unmatched_references_are_reported_without_manufacturing_edges(self):
        self.http.get.return_value = {'message':{'DOI':DOI,'reference':[]}}
        self.library.pdf.return_value = Path('a.pdf')
        with patch('paper_server.citation_fallback.extract_references',return_value=[reference_entry('An unresolved standard',1,7)]):
            result = self.provider.fetch(DOI)
        self.assertEqual(len(result['works']),1)
        self.assertEqual(result['meta']['reference_unresolved'],1)
        self.assertEqual(build_citation_graph(result)['lineage_edges'],[])

    def test_crossref_failure_still_allows_pdf_fallback(self):
        self.http.get.side_effect = SourceUnavailable('crossref_unavailable')
        self.library.pdf.return_value = Path('a.pdf')
        self.library.match.return_value = local('202','10.1234/ref')
        with patch('paper_server.citation_fallback.extract_references',return_value=[reference_entry('H. Liu, “A precise optical modulator study,” 2024.',1,2)]):
            result = self.provider.fetch(DOI)
        self.assertEqual(result['meta']['reference_origin'],'pdf')
        self.assertEqual(result['meta']['reference_fetched'],1)

    def test_reference_matching_requires_title_author_and_year_consistency(self):
        entry = reference_entry('H. Liu et al., “A precise optical modulator study,” 2024.',1,13)
        self.assertEqual(entry['author'],'Liu')
        candidate = sql_work(local('202','10.1234/ref'))
        self.assertTrue(matched(entry,candidate))
        self.assertFalse(matched({**entry,'author':'Wrong'},candidate))
        self.assertFalse(matched({**entry,'year':2020},candidate))
        self.assertFalse(matched({**entry,'title':'A different optical modulator study'},candidate))

    def test_ieee_zero_metadata_skips_both_citation_list_requests(self):
        self.provider.ieee_key = 'test-key'
        self.source = snapshot(work(1,[2],doi=DOI),work(2))
        self.http.get.return_value = {'articles':[{'article_number':'101','doi':DOI,'citing_paper_count':0}]}
        result = self.provider.fetch(DOI)
        self.assertEqual(self.http.get.call_count,1)
        self.assertEqual(result['meta']['ieee']['total'],0)

    def test_ieee_citers_are_deduplicated_by_doi_and_add_incoming_edges(self):
        self.provider.ieee_key = 'test-key'
        self.source = snapshot(work(1,[2],doi=DOI,citation_count=1),work(2),work(3,doi='10.1234/citer'))
        record = {'title':'An incoming citer','displayText':'H. Liu, "An incoming citer," 2025.',
                  'links':{'doi':'https://doi.org/10.1234/citer','articleNumber':'303'}}
        self.http.get.side_effect = [
            {'articles':[{'article_number':'101','doi':DOI,'citing_paper_count':2}]},
            {'article_number':'101','ieee_citation_count':1,'ieee_citation':[record]},
            {'article_number':'101','non_ieee_citation_count':1,'non_ieee_citation':[record]},
        ]
        result = self.provider.fetch(DOI)
        self.assertEqual(len(result['works']),3)
        self.assertEqual(result['works'][0]['citation_count'],1)
        self.assertEqual(result['meta']['ieee_citation_count'],2)
        graph = build_citation_graph(result)
        incoming = next(edge for edge in graph['lineage_edges'] if edge['source']=='W1')
        self.assertEqual(incoming['target'],'W3')
        self.assertEqual(incoming['reference_source'],'ieee')
        self.assertEqual(self.http.get.call_count,3)

    def test_ieee_quota_keeps_openalex_graph_without_retrying(self):
        self.provider.ieee_key = 'test-key'
        self.source = snapshot(work(1,[2],doi=DOI),work(2))
        self.http.get.side_effect = SourceUnavailable('ieee_local_budget',429)
        result = self.provider.fetch(DOI)
        self.assertEqual(result['meta']['ieee']['status'],'deferred')
        self.assertEqual(len(build_citation_graph(result)['nodes']),2)
        self.assertEqual(self.http.get.call_count,1)

    def test_legacy_cache_upgrades_without_refetching_openalex(self):
        self.source = snapshot(work(1,[2],doi=DOI),work(2))
        with TemporaryDirectory() as directory:
            service = CitationGraphService(self.provider,directory)
            self.addCleanup(service.close)
            service._store(DOI,self.source)
            self.assertEqual(service.get(DOI,cached_only=True)[1],200)
            self.http.get.assert_not_called()
            self.assertEqual(service.get(DOI)[1],202)
            service.jobs[DOI].result(timeout=5)
            self.assertEqual(service.get(DOI)[1],200)
        self.primary.fetch.assert_not_called()

    def test_pdf_paths_cannot_escape_the_publisher_directory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'ieee-pdf').mkdir()
            (root/'outside.pdf').write_bytes(b'pdf')
            library = CitationLibrary(Mock(),root)
            self.assertIsNone(library.pdf({**self.seed_row,'pdf_local_path':'outside.pdf'}))
            (root/'ieee-pdf/101.pdf').write_bytes(b'pdf')
            self.assertEqual(library.pdf(self.seed_row),root/'ieee-pdf/101.pdf')

    def test_pdf_parser_requires_a_numbered_references_section(self):
        reader=Mock(pages=[Mock(extract_text=Mock(return_value='Introduction\n[1] Not a bibliography')),
                          Mock(extract_text=Mock(return_value='REFERENCES\n[1] H. Liu, “Paper one,” 2024.\n[2] A. Reed, “Paper two,” 2023.'))])
        with patch('paper_server.citation_fallback.PdfReader',return_value=reader):
            entries=extract_references('a.pdf')
        self.assertEqual(len(entries),2)
        self.assertEqual([entry['number'] for entry in entries],[1,2])
        self.assertEqual(entries[0]['page'],2)
        reader.pages[1].extract_text.return_value='REFERENCES\n[1] First\n[1] Duplicate'
        with patch('paper_server.citation_fallback.PdfReader',return_value=reader):
            self.assertEqual(extract_references('a.pdf'),[])

    def test_crossref_can_seed_an_external_doi_not_indexed_by_openalex(self):
        self.primary.fetch.side_effect=ProviderError('Not indexed',404)
        self.library.by_doi.return_value=None
        self.library.by_doi.side_effect=None
        self.http.get.return_value={'message':{'DOI':DOI,'title':['External seed'],'reference':[]}}
        result=self.provider.fetch(DOI)
        self.assertEqual(result['works'][0]['title'],'External seed')
        self.assertEqual(result['works'][0]['metadata_source'],'crossref')
        self.assertEqual(build_citation_graph(result)['nodes'][0]['citation_source'],None)


if __name__ == '__main__':
    unittest.main()
