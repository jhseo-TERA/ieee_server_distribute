"""Pure integration regressions: no MySQL, services, or model inference."""
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock

from local_ai_service import LocalAIService, validate_job


class LocalAiIntegrationEdgeTests(unittest.TestCase):
    def setUp(self):
        self.repo, self.runtime, self.docs = Mock(), Mock(), Mock()
        self.service = LocalAIService(self.repo, self.runtime, self.docs, Path('.'))

    def test_json_enum_objects_are_validation_errors_not_uncaught_type_errors(self):
        for change in ({'kind': []}, {'kind': {}}, {'model': {}}, {'model': ['gpt-oss:20b']},
                       {'scope': []}, {'scope': {}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_job({'question': 'A question', **change}, role='admin')

    def test_explicit_wrong_page_and_filter_shapes_do_not_silently_default(self):
        for change in ({'pages': 0}, {'pages': False}, {'pages': ''}, {'filters': []},
                       {'filters': False}, {'filters': ''}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_job({'question': 'A question', **change}, role='admin')

    def test_grounding_includes_sql_matching_operating_point(self):
        paper = {'id': 1, 'article_number': '123', 'title': 'Receiver paper',
                 'pdf_available': False, 'matching_measurements': [
                     {'id': 99, 'lane_rate_gbps': 112.25, 'operating_point_key': 'matched-lane',
                      'rate_scope': 'lane', 'source_kind': 'user_sheet'}]}
        # A legacy low-rate operating point precedes the SQL-matched later point.
        self.repo.paper_evidence.return_value = {'paper': paper, 'measurements': [
            {'id': i, 'lane_rate_gbps': 10, 'operating_point_key': f'legacy-{i}',
             'rate_scope': 'lane'} for i in range(1, 5)] + paper['matching_measurements']}
        sources, _ = self.service._sources([paper], 'Find fast receivers', 6000)
        self.assertIn('112.25', '\n'.join(source['text'] for source in sources))

    def test_source_budget_is_hard_limit_for_comparisons(self):
        papers = [{'id': n, 'article_number': str(n), 'title': f'Paper {n}',
                   'pdf_available': True, 'abstract_text': 'abstract ' * 120} for n in (1, 2)]
        self.repo.paper_evidence.return_value = {'measurements': []}
        self.docs.read_pages.return_value = [{'page': 1, 'text': 'evidence'}]
        self.docs.retrieve.return_value = [{'page': 1, 'text': 'a' * 1000},
                                           {'page': 2, 'text': 'b' * 1000}]
        sources, _ = self.service._sources(papers, 'Compare', 1200)
        self.assertLessEqual(sum(len(source['text']) for source in sources), 1200)
        self.assertEqual({source['article_number'] for source in sources if source['kind'] == 'database'}, {'1', '2'})

    def test_cancelled_extraction_does_not_persist_proposals(self):
        paper = {'id': 1, 'article_number': '123', 'title': 'Receiver', 'pdf_available': True}
        self.repo.get_papers.return_value = [paper]
        self.docs.read_pages.return_value = [{'page': 1, 'text': '7 nm CMOS',
                                              'source_url': '/pdf/123#page=1', 'pdf_sha256': 'a' * 64}]
        request = validate_job({'kind': 'extract', 'article_numbers': ['123'], 'vision': True}, 'admin')
        event = threading.Event()
        event.set()
        with self.assertRaises(RuntimeError):
            self.service._extract('job', request, event)
        self.runtime.chat.assert_not_called()
        self.repo.add_proposals.assert_not_called()


if __name__ == '__main__':
    unittest.main()
