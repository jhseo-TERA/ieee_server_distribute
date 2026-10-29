"""Offline cache/job tests for the external citation metadata boundary."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import Mock, patch

from paper_server.citation_graph import CACHE_TTL, EMPTY_CACHE_TTL, CitationGraphService
from paper_server.citation_provider import ProviderError
from tests.test_citation_graph import snapshot, work


DOI = '10.1234/paper-1'


class CitationServiceTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / 'tmp'
        temporary_root.mkdir(exist_ok=True)
        self.directory = self.enterContext(TemporaryDirectory(dir=temporary_root))
        self.source = snapshot(work(1, [100]), work(2, [100]))
        self.provider = Mock()
        self.provider.fetch.side_effect = lambda doi: deepcopy(self.source)
        self.service = CitationGraphService(self.provider, self.directory, max_workers=1)
        self.addCleanup(self.service.close)

    def finish(self, doi=DOI):
        future = self.service.jobs.get(doi)
        if future is not None:
            future.result(timeout=5)
        return self.service.get(doi)

    def test_pending_same_doi_is_deduplicated_and_then_cached(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.provider.fetch.side_effect = lambda doi: (gate.wait(5), deepcopy(self.source))[1]
        body, status = self.service.get(DOI)
        self.assertEqual(status, 202)
        self.assertEqual(body['status'], 'building')
        _, repeated_status = self.service.get('https://doi.org/10.1234/PAPER-1')
        self.assertEqual(repeated_status, 202)
        self.assertEqual(len(self.service.jobs), 1)
        gate.set()
        graph, status = self.finish()
        self.assertEqual(status, 200)
        self.assertEqual(graph['seed_id'], 'W1')
        self.service.get(DOI)
        self.assertEqual(self.provider.fetch.call_count, 1)

    def test_disk_cache_survives_service_restart_and_does_not_store_account_fields(self):
        self.source['works'][0].update(is_favorite=True, account_id=12,
                                      pdf_local_path='private.pdf')
        self.service.get(DOI)
        graph, _ = self.finish()
        graph['nodes'][0].update(is_favorite=True, article_number='secret-account-state')
        graph['meta']['library_count'] = 2
        cached = json.loads(self.service._path(DOI).read_text(encoding='utf-8'))
        serialized = json.dumps(cached)
        for private in ('is_favorite', 'account_id', 'pdf_local_path',
                        'secret-account-state', 'library_count'):
            self.assertNotIn(private, serialized)
        other_provider = Mock()
        other = CitationGraphService(other_provider, self.directory)
        self.addCleanup(other.close)
        result, status = other.get(DOI)
        self.assertEqual(status, 200)
        self.assertNotIn('is_favorite', result['nodes'][0])
        other_provider.fetch.assert_not_called()

    def test_expired_cache_enqueues_fresh_provider_lookup(self):
        self.service.get(DOI)
        self.finish()
        now = time.time()
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.provider.fetch.side_effect = lambda doi: (gate.wait(5), deepcopy(self.source))[1]
        with patch('paper_server.citation_graph.time.time', return_value=now + CACHE_TTL + 1):
            _, status = self.service.get(DOI)
        self.assertEqual(status, 202)
        gate.set()
        self.finish()
        self.assertEqual(self.provider.fetch.call_count, 2)

    def test_provider_rate_limit_has_cooldown_without_repeated_external_requests(self):
        self.provider.fetch.side_effect = ProviderError('Provider budget reached.', 429)
        self.service.get(DOI)
        with self.assertRaises(ProviderError):
            self.service.jobs[DOI].result(timeout=5)
        body, status = self.service.get(DOI)
        self.assertEqual(status, 429)
        self.assertGreaterEqual(body['retry_after'], 298)
        self.assertEqual(self.service.get(DOI)[1], 429)
        self.assertEqual(self.provider.fetch.call_count, 1)

    def test_partial_result_recovers_after_five_minutes_in_memory_and_on_disk(self):
        self.source['meta']['warnings'] = ['Temporary citation lookup failure.']
        self.service.get(DOI)
        graph, status = self.finish()
        self.assertEqual(status, 200)
        self.assertEqual(graph['meta']['cache_ttl_seconds'], 300)
        restarted_directory = Path(self.directory) / 'restart'
        restarted_directory.mkdir()
        other = CitationGraphService(self.provider, restarted_directory)
        self.addCleanup(other.close)
        other._path(DOI).write_text(self.service._path(DOI).read_text(encoding='utf-8'),
                                   encoding='utf-8')
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.provider.fetch.side_effect = lambda doi: (gate.wait(5), deepcopy(self.source))[1]
        now = time.time()
        with patch('paper_server.citation_graph.time.time', return_value=now + 301):
            self.assertEqual(self.service.get(DOI)[1], 202)
            self.assertEqual(other.get(DOI)[1], 202)

    def test_seed_only_cache_rechecks_after_a_day_in_memory_and_on_disk(self):
        self.source = snapshot(work(1))
        self.service.get(DOI)
        graph, status = self.finish()
        self.assertEqual(status, 200)
        self.assertEqual(len(graph['nodes']), 1)
        self.assertEqual(graph['meta']['cache_ttl_seconds'], EMPTY_CACHE_TTL)
        other = CitationGraphService(self.provider, self.directory)
        self.addCleanup(other.close)
        self.assertEqual(other.get(DOI, cached_only=True)[1], 200)
        self.assertEqual(self.provider.fetch.call_count, 1)
        gate = threading.Event()
        self.addCleanup(gate.set)
        enriched = snapshot(work(1, [2]), work(2))
        self.provider.fetch.side_effect = lambda doi: (gate.wait(5), deepcopy(enriched))[1]
        now = time.time()
        with patch('paper_server.citation_graph.time.time', return_value=now + EMPTY_CACHE_TTL + 1):
            self.assertEqual(self.service.get(DOI)[1], 202)
            self.assertEqual(other.get(DOI)[1], 202)
        gate.set()
        graph, status = self.finish()
        self.assertEqual(status, 200)
        self.assertEqual(len(graph['nodes']), 2)
        self.assertEqual(graph['meta']['cache_ttl_seconds'], CACHE_TTL)

    def test_unexpected_provider_errors_do_not_disclose_request_or_secret(self):
        secret = 'DO-NOT-DISCLOSE-KEY'
        self.provider.fetch.side_effect = RuntimeError(
            'https://api.openalex.org/works?api_key=' + secret)
        self.service.get(DOI)
        with self.assertRaises(RuntimeError):
            self.service.jobs[DOI].result(timeout=5)
        with self.assertLogs('paper_server.citation_graph', level='WARNING') as logs:
            body, status = self.service.get(DOI)
        self.assertEqual(status, 503)
        self.assertNotIn(secret, json.dumps(body))
        self.assertNotIn(secret, '\n'.join(logs.output))
        self.assertGreater(body['retry_after'], 0)

    def test_queue_is_bounded_even_for_distinct_dois(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.provider.fetch.side_effect = lambda doi: (gate.wait(5), deepcopy(self.source))[1]
        for number in range(8):
            self.assertEqual(self.service.get(f'10.1234/queued-{number}')[1], 202)
        body, status = self.service.get('10.1234/queued-overflow')
        self.assertEqual(status, 429)
        self.assertEqual(len(self.service.jobs), 8)
        self.assertGreater(body['retry_after'], 0)

    def test_invalid_doi_does_not_start_a_job_or_create_cache(self):
        for doi in ('http://127.0.0.1/admin', '10.1234/a\x00b', 'not a doi'):
            with self.subTest(doi=doi), self.assertRaises(ValueError):
                self.service.get(doi)
        self.assertEqual(self.service.jobs, {})
        self.provider.fetch.assert_not_called()
        self.assertEqual(list(Path(self.directory).iterdir()), [])

    def test_malformed_cache_is_recovered_by_refetch_instead_of_crashing(self):
        self.service._path(DOI).write_text('[]', encoding='utf-8')
        _, status = self.service.get(DOI)
        self.assertEqual(status, 202)
        self.assertEqual(self.finish()[1], 200)

    def test_abandoned_failed_job_keeps_cooldown_when_another_doi_is_requested(self):
        self.provider.fetch.side_effect = ProviderError('Provider budget reached.', 429)
        self.service.get(DOI)
        with self.assertRaises(ProviderError):
            self.service.jobs[DOI].result(timeout=5)
        self.service.get('10.1234/some-other-paper')
        body, status = self.service.get(DOI)
        self.assertEqual(status, 429)
        self.assertGreater(body['retry_after'], 0)


if __name__ == '__main__':
    unittest.main()
