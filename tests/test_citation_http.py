from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock

import requests

from paper_server.citation_http import CitationHTTP, SourceUnavailable


URL = 'https://ieeexploreapi.ieee.org/api/v1/search/articles'


def response(data=None, status=200, headers=None):
    return Mock(status_code=status, headers=headers or {}, json=Mock(return_value=data or {'ok': True}))


class CitationHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(TemporaryDirectory())
        self.path = Path(self.directory)/'state.sqlite3'
        self.session = Mock()
        self.session.get.return_value = response()
        self.http = CitationHTTP(self.path, self.session, ieee_limit=2, interval=0)

    def get(self, query='one', http=None):
        return (http or self.http).get('ieee', URL, {'querytext':query}, key='private-test-key')

    def test_cache_survives_restart_and_cache_hits_cost_no_attempts(self):
        self.get()
        restarted = CitationHTTP(self.path, self.session, ieee_limit=2, interval=0)
        self.get(http=restarted)
        self.assertEqual(self.session.get.call_count, 1)
        self.assertEqual(restarted.budget('private-test-key')['used'], 1)
        self.assertNotIn(b'private-test-key', self.path.read_bytes())

    def test_budget_is_shared_by_separate_instances_and_fails_closed(self):
        self.get('one')
        other = CitationHTTP(self.path, self.session, ieee_limit=2, interval=0)
        self.get('two', other)
        with self.assertRaises(SourceUnavailable) as caught:
            self.get('three')
        self.assertEqual(caught.exception.reason, 'ieee_local_budget')
        self.assertEqual(self.session.get.call_count, 2)

    def test_simultaneous_reservations_cannot_exceed_the_limit(self):
        def request(index):
            try:
                self.get(str(index))
                return True
            except SourceUnavailable:
                return False
        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(request, range(6)))
        self.assertEqual(sum(outcomes), 2)
        self.assertEqual(self.session.get.call_count, 2)

    def test_same_request_is_single_flight(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.session.get.side_effect = lambda *args, **kwargs: (entered.set(), release.wait(3), response())[2]
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(self.get)
            self.assertTrue(entered.wait(3))
            with self.assertRaises(SourceUnavailable) as caught:
                self.get()
            self.assertEqual(caught.exception.reason, 'provider_request_pending')
            release.set()
            pending.result(timeout=3)
        self.assertEqual(self.session.get.call_count, 1)

    def test_server_errors_count_once_and_are_not_retried(self):
        self.session.get.return_value = response(status=500)
        for _ in range(2):
            with self.assertRaises(SourceUnavailable):
                self.get()
        self.assertEqual(self.session.get.call_count, 1)
        self.assertEqual(self.http.budget('private-test-key')['used'], 1)

    def test_429_blocks_other_ieee_endpoints_without_more_calls(self):
        self.session.get.return_value = response(status=429, headers={'Retry-After':'10'})
        with self.assertRaises(SourceUnavailable):
            self.get('first')
        with self.assertRaises(SourceUnavailable) as caught:
            self.get('second')
        self.assertEqual(caught.exception.reason, 'ieee_cooldown')
        self.assertEqual(self.session.get.call_count, 1)

    def test_attempts_expire_on_a_rolling_24_hour_window(self):
        now = [100000.0]
        http = CitationHTTP(self.path,self.session,clock=lambda:now[0],ieee_limit=1,interval=0)
        self.get('one',http)
        now[0] += 86401
        self.get('two',http)
        self.assertEqual(http.budget('private-test-key')['used'],1)

    def test_request_spacing_is_reserved_persistently(self):
        sleep = Mock()
        http = CitationHTTP(self.path,self.session,clock=lambda:100000,sleep=sleep,ieee_limit=2,interval=2)
        self.get('one',http)
        self.get('two',http)
        sleep.assert_called_once_with(2)

    def test_connection_error_and_key_are_never_disclosed_or_persisted(self):
        self.session.get.side_effect = requests.ConnectionError('https://host/?apikey=private-test-key')
        with self.assertRaises(SourceUnavailable) as caught:
            self.get()
        self.assertNotIn('private-test-key',str(caught.exception))
        self.assertNotIn(b'private-test-key',self.path.read_bytes())

    def test_fixed_hosts_only_and_zero_budget_disables_network(self):
        with self.assertRaises(SourceUnavailable):
            self.http.get('ieee','https://evil.example/',key='private-test-key')
        disabled = CitationHTTP(self.path,self.session,ieee_limit=0,interval=0)
        with self.assertRaises(SourceUnavailable):
            self.get(http=disabled)
        self.session.get.assert_not_called()

    def test_crossref_does_not_consume_the_ieee_budget(self):
        self.http.get('crossref','https://api.crossref.org/works/10.1234%2Fa')
        self.assertEqual(self.http.budget('private-test-key')['used'],0)

    def test_success_body_echoing_a_key_is_not_cached(self):
        self.session.get.return_value=response({'api_key':'private-test-key'})
        with self.assertRaises(SourceUnavailable):
            self.get()
        self.assertNotIn(b'private-test-key',self.path.read_bytes())


if __name__ == '__main__':
    unittest.main()
