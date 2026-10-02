import json
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock

from local_ai_service import LocalAIService, MODELS, validate_extractions, validate_job


def page(text='This receiver is implemented in 7-nm CMOS.'):
    return {'page': 1, 'text': text, 'pdf_sha256': 'a' * 64, 'source_url': '/pdf/123#page=1'}


def extraction(**changes):
    row = {'field': 'process_nm', 'value': 7, 'unit': 'nm', 'page': 1,
           'quote': 'This receiver is implemented in 7-nm CMOS.',
           'operating_point': '112G nominal', 'rate_scope': 'unknown',
           'power_scope': 'unknown', 'component_scope': 'rx', 'notes': ''}
    row.update(changes)
    return row


class JobValidationTests(unittest.TestCase):
    def test_defaults_and_selected_papers(self):
        job = validate_job({'question': 'Compare these papers', 'article_numbers': ['123', '123']})
        self.assertEqual(job['model'], MODELS[0])
        self.assertEqual(job['article_numbers'], ['123'])
        self.assertEqual(job['num_ctx'], 8192)

    def test_viewer_cannot_extract(self):
        with self.assertRaises(PermissionError):
            validate_job({'kind': 'extract', 'article_numbers': ['123']})

    def test_rejects_arbitrary_model_path_and_unsupported_options(self):
        for changes in ({'model': 'http://evil.invalid/model'}, {'article_numbers': ['../secret']},
                        {'pages': [True]}, {'max_tokens': 100000}, {'num_ctx': True},
                        {'filters': {'sql': 'DROP TABLE papers'}}, {'vision': 'false'},
                        {'question': 'x' * 2001}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_job({'question': 'test', **changes}, role='admin')

    def test_vision_requires_qwen_extract(self):
        with self.assertRaises(ValueError):
            validate_job({'kind': 'extract', 'article_numbers': ['123'],
                          'model': MODELS[0], 'vision': True}, role='admin')
        job = validate_job({'kind': 'extract', 'article_numbers': ['123'],
                            'vision': True, 'pages': [3, 1]}, role='admin')
        self.assertEqual(job['pages'], [1, 3])
        self.assertEqual(job['model'], MODELS[1])


class ExtractionGroundingTests(unittest.TestCase):
    def test_exact_quote_numeric_unit_and_pdf_revision(self):
        row = validate_extractions([extraction()], [page()])[0]
        self.assertEqual(row['validation_status'], 'valid')
        self.assertEqual(row['source_sha256'], 'a' * 64)
        self.assertEqual(row['source_url'], '/pdf/123#page=1')

    def test_hallucinated_quote_or_value_blocked(self):
        for change in ({'quote': 'A different quote reports 7-nm CMOS.'}, {'value': 9},
                       {'unit': 'um'}, {'page': 2}, {'value': None},
                       {'value': float('nan')}, {'value': True}, {'value': -7}):
            with self.subTest(change=change):
                row = validate_extractions([extraction(**change)], [page()])[0]
                self.assertEqual(row['validation_status'], 'invalid')

    def test_visual_quote_is_not_auto_verified(self):
        row = validate_extractions([extraction()], [page('')], vision=True)[0]
        self.assertEqual(row['validation_status'], 'visual_review')
        self.assertIn('quote_requires_visual_check', row['validation_reasons'])

    def test_vision_does_not_excuse_invalid_units_or_scope(self):
        row = validate_extractions([extraction(unit='um')], [page('')], vision=True)[0]
        self.assertEqual(row['validation_status'], 'invalid')
        row = validate_extractions([extraction(field='lane_rate_gbps', value=112,
            unit='Gbps', quote='A receiver achieves 112 Gb/s.', rate_scope='aggregate')],
            [page('A receiver achieves 112 Gb/s.')], vision=True)[0]
        self.assertEqual(row['validation_status'], 'invalid')

    def test_bounded_records_and_quote_size(self):
        rows = validate_extractions([extraction()] * 100, [page()])
        self.assertEqual(len(rows), 24)
        row = validate_extractions([extraction(quote='x' * 1801)], [page('x' * 1801)])[0]
        self.assertEqual(row['validation_status'], 'invalid')

    def test_plain_rate_cannot_inherit_model_claimed_lane_scope(self):
        quote = 'A receiver achieves 112 Gb/s.'
        record = extraction(field='lane_rate_gbps', value=112, unit='Gbps',
                            quote=quote, rate_scope='lane')
        self.assertEqual(validate_extractions([record], [page(quote)])[0]['validation_status'], 'invalid')
        self.assertEqual(validate_extractions([record], [page(quote)], vision=True)[0]['validation_status'], 'visual_review')

    def test_explicit_per_lane_quote_is_grounded(self):
        quote = 'A receiver achieves 112 Gb/s per lane.'
        record = extraction(field='lane_rate_gbps', value=112, unit='Gbps',
                            quote=quote, rate_scope='lane')
        self.assertEqual(validate_extractions([record], [page(quote)])[0]['validation_status'], 'valid')

    def test_energy_and_power_cannot_claim_receiver_from_transmitter_quote(self):
        for field, value, unit, quote in (
            ('energy_pj_bit', 2, 'pJ/bit', 'This transmitter consumes 2 pJ/bit.'),
            ('power_mw', 30, 'mW', 'This transmitter consumes 30 mW.'),
        ):
            with self.subTest(field=field):
                record = extraction(field=field, value=value, unit=unit, quote=quote,
                                    component_scope='rx')
                self.assertEqual(validate_extractions([record], [page(quote)])[0]['validation_status'], 'invalid')

    def test_missing_component_grounding_is_review_only_or_unknown(self):
        quote = 'The measured energy efficiency is 2 pJ/bit.'
        record = extraction(field='energy_pj_bit', value=2, unit='pJ/bit', quote=quote,
                            component_scope='rx')
        self.assertEqual(validate_extractions([record], [page(quote)])[0]['validation_status'], 'invalid')
        self.assertEqual(validate_extractions([record], [page(quote)], vision=True)[0]['validation_status'], 'visual_review')
        unknown = {**record, 'component_scope': 'unknown'}
        self.assertEqual(validate_extractions([unknown], [page(quote)])[0]['validation_status'], 'valid')


class ServiceGroundingTests(unittest.TestCase):
    def setUp(self):
        self.repo, self.runtime, self.documents = Mock(), Mock(), Mock()
        self.paper = {'id': 1, 'article_number': '123', 'title': 'Test receiver',
                      'year': '2025', 'pdf_available': False, 'abstract_text': 'Verified metadata'}
        self.repo.get_papers.return_value = [self.paper]
        self.repo.paper_evidence.return_value = {'paper': self.paper, 'measurements': []}
        self.service = LocalAIService(self.repo, self.runtime, self.documents, Path('.'))
        self.req = validate_job({'question': 'Compare this paper', 'article_numbers': ['123']})

    def response(self, answer, citations):
        self.runtime.chat.return_value = {'content': json.dumps({
            'answer': answer, 'citations': citations, 'limitations': []}), 'usage': {}}

    def test_unknown_citations_flagged_not_silently_accepted(self):
        self.response('Unsupported claim [S999]', ['S999'])
        result = self.service.run_job('job', self.req, threading.Event())
        self.assertEqual(result['grounding'], 'needs_review')
        self.assertEqual(result['citations'], [])
        self.assertTrue(any('출처 ID' in reason for reason in result['limitations']))
        self.repo.add_proposals.assert_not_called()
        self.repo.decide_proposals.assert_not_called()

    def test_existing_citations_and_no_pdf_limitation(self):
        self.response('Metadata-backed description [S1]', ['S1'])
        result = self.service.run_job('job', self.req, threading.Event())
        self.assertEqual(result['citations'], ['S1'])
        self.assertTrue(any('로컬 PDF' in reason for reason in result['limitations']))
        self.documents.read_pages.assert_not_called()

    def test_superlatives_are_not_treated_as_verified_by_citation_id(self):
        self.response('The lowest energy receiver [S1]', ['S1'])
        result = self.service.run_job('job', self.req, threading.Event())
        self.assertEqual(result['grounding'], 'needs_review')
        self.assertTrue(any('비교 우위' in reason for reason in result['limitations']))

    def test_cancellation_owner_look_up_happens_first(self):
        self.repo.get_job.return_value = None
        with self.assertRaises(KeyError):
            self.service.cancel('job', 'other-owner')
        self.repo.get_job.assert_called_once_with('job', 'other-owner')
        self.repo.update_job.assert_not_called()

    def test_sources_prioritize_exact_matching_measurements(self):
        measurements = [dict(id=i, source_kind='abstract', review_status='extracted',
                             process_nm=7, overall_confidence=0.8) for i in range(10)]
        matching = dict(id=999, source_kind='pdf', review_status='verified',
                        lane_rate_gbps=112, overall_confidence=1)
        self.repo.paper_evidence.return_value = {'measurements': measurements + [matching]}
        papers = [{**self.paper, 'matching_measurements': [matching]}]
        sources, _ = self.service._sources(papers, '112 Gbps', 20000)
        points = json.loads(sources[0]['text'])['measurements']
        self.assertIn(999, [point['id'] for point in points])

    def test_rejected_or_review_required_points_do_not_enter_answer_sources(self):
        measurements = [dict(id=i, source_kind='pdf', review_status=status)
                        for i, status in enumerate(['rejected', 'invalid', 'needs_review', 'verified'])]
        self.repo.paper_evidence.return_value = {'measurements': measurements}
        sources, _ = self.service._sources([self.paper], 'question', 20000)
        points = json.loads(sources[0]['text'])['measurements']
        self.assertTrue(points)
        self.assertFalse(any(point['review_status'] in {'rejected', 'invalid', 'needs_review'} for point in points))


if __name__ == '__main__':
    unittest.main()
