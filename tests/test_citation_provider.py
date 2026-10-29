"""Offline boundary and partial-data tests for public citation retrieval."""
import unittest
from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import Mock, patch

import requests

from paper_server.citation_provider import (
    MAX_CANDIDATES, MAX_WORK_REFERENCES, OpenAlexProvider, ProviderError,
    _abstract, _normalize_work, normalize_doi,
)


DOI = "10.1109/jssc.2024.3435378"


def work(identifier, references=(), doi=None, **extra):
    return {"id": f"https://openalex.org/W{identifier}", "doi": doi,
            "title": f"Paper {identifier}", "referenced_works": [f"https://openalex.org/W{i}" for i in references],
            "related_works": [], "publication_year": 2024, "cited_by_count": 0, **extra}


def response(payload=None, status=200, headers=None):
    return Mock(status_code=status, headers=headers or {}, json=Mock(return_value=payload))


def listed(works, count=None):
    return response({"results": works, "meta": {"count": len(works) if count is None else count}})


class CitationProviderTests(unittest.TestCase):
    def provider(self, responses):
        session = Mock()
        session.get.side_effect = responses
        return OpenAlexProvider(session=session, api_key=""), session

    def test_doi_normalization_and_validation(self):
        for value in [DOI.upper(), f"https://doi.org/{DOI}", f"http://dx.doi.org/{DOI}",
                      f"doi: {DOI}", f"doi.org/{DOI}"]:
            self.assertEqual(normalize_doi(value), DOI)
        for value in [None, "", "https://example.com/10.1109/foo", "10.1/foo", "10.1109/foo bar",
                      "10.1109/foo\n", "10.1109/foo\tbar", "10.1109/a\u200db", "10.1109/" + "x" * 256]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_doi(value)
        self.assertEqual(normalize_doi("10.1002/(sici)foo:bar;abc"), "10.1002/(sici)foo:bar;abc")

    def test_fetch_maps_public_schema_and_observed_references(self):
        seed = work(1, [2, 3], doi=f"https://doi.org/{DOI}", cited_by_count=1,
                    related_works=["https://openalex.org/W4"])
        provider, session = self.provider([
            response(seed), listed([work(2), work(3), work(4)]),
            listed([work(5, [1, 2])]), listed([work(6, [2, 3])]),
        ])
        data = provider.fetch(DOI)
        self.assertEqual(data["seed_id"], "W1")
        self.assertEqual([item["id"] for item in data["works"]], ["W1", "W2", "W3", "W4", "W5", "W6"])
        self.assertEqual(data["works"][0]["doi"], DOI)
        self.assertEqual(data["meta"]["request_count"], 4)
        self.assertEqual(data["meta"]["reference_fetched"], 2)
        self.assertFalse(data["meta"]["coverage_truncated"])
        for call in session.get.call_args_list:
            self.assertTrue(call.args[0].startswith("https://api.openalex.org/works"))
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertEqual(call.kwargs["timeout"], (3.05, 12))

    def test_query_fragment_and_path_characters_are_encoded(self):
        special = "10.1109/test?query=1#fragment"
        provider, session = self.provider([response(work(1, doi=special)), listed([])])
        provider.fetch(special)
        url = session.get.call_args_list[0].args[0]
        self.assertNotIn("?", url)
        self.assertNotIn("#", url)
        self.assertIn("%3Fquery%3D1%23fragment", url)

    def test_seed_identity_must_match_requested_doi(self):
        provider, _ = self.provider([response(work(1, doi="10.1109/wrong"))])
        with self.assertRaises(ProviderError) as caught:
            provider.fetch(DOI)
        self.assertEqual(caught.exception.status, 502)

    def test_partial_graph_records_unavailable_references(self):
        provider, session = self.provider([
            response(work(1, [2, 3], doi=DOI, cited_by_count=1)),
            response(status=429, headers={"Retry-After": "60"}), listed([work(5, [1])]),
        ])
        data = provider.fetch(DOI)
        self.assertEqual(data["meta"]["reference_fetched"], 0)
        self.assertTrue(data["meta"]["coverage_truncated"])
        self.assertTrue(data["meta"]["warnings"])
        self.assertEqual(len(data["works"]), 2)
        self.assertEqual(session.get.call_count, 3)

    def test_key_is_header_only_and_error_does_not_expose_it(self):
        session = Mock()
        session.get.side_effect = requests.ConnectionError("secret-key-leaked-in-upstream-error")
        provider = OpenAlexProvider(session=session, api_key="private-key")
        with patch("paper_server.citation_provider.time.sleep"), self.assertRaises(ProviderError) as caught:
            provider.fetch(DOI)
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(session.get.call_count, 2)
        call = session.get.call_args
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer private-key")
        self.assertNotIn("api_key", call.kwargs["params"])

    def test_nontransient_errors_and_redirects_never_retry(self):
        for status in [301, 400, 401, 403, 404]:
            provider, session = self.provider([response(status=status)])
            with self.subTest(status=status), self.assertRaises(ProviderError):
                provider.fetch(DOI)
            self.assertEqual(session.get.call_count, 1)

    def test_transient_error_recovers_once(self):
        provider, session = self.provider([response(status=503), response(work(1, doi=DOI)), listed([])])
        with patch("paper_server.citation_provider.time.sleep"):
            result = provider.fetch(DOI)
        self.assertEqual(result["meta"]["request_count"], 3)
        self.assertEqual(session.get.call_count, 3)

    def test_invalid_response_is_safe(self):
        bad_json = response()
        bad_json.json.side_effect = ValueError("potentially sensitive response body")
        for result in [bad_json, response([])]:
            provider, _ = self.provider([result])
            with self.assertRaises(ProviderError) as caught:
                provider.fetch(DOI)
            self.assertNotIn("sensitive", str(caught.exception))

    def test_large_neighborhood_is_bounded_and_coverage_is_explicit(self):
        seed = work(1, range(2, 202), doi=DOI, cited_by_count=1000,
                    related_works=[f"https://openalex.org/W{i}" for i in range(1000, 1010)])
        provider, session = self.provider([
            response(seed), listed([work(i) for i in list(range(2, 82)) + list(range(1000, 1010))]),
            listed([work(i, [1]) for i in range(2000, 2060)], 1000),
            listed([work(i, [2]) for i in range(3000, 3100)], 400),
        ])
        data = provider.fetch(DOI)
        self.assertEqual(len(data["works"]), MAX_CANDIDATES)
        self.assertTrue(data["meta"]["reference_truncated"])
        self.assertTrue(data["meta"]["citing_truncated"])
        self.assertEqual(data["meta"]["reference_total"], 200)
        self.assertEqual(data["meta"]["citing_fetched"], 60)
        batch = session.get.call_args_list[1].kwargs["params"]["filter"].split(":", 1)[1].split("|")
        self.assertEqual(len(batch), 90)
        self.assertEqual(session.get.call_args_list[3].kwargs["params"]["per_page"], 29)

    def test_null_metadata_and_large_reference_arrays_are_safe(self):
        raw = work(1, range(2, MAX_WORK_REFERENCES + 9), doi=None, primary_location=None,
                   authorships=None, cited_by_count=None, publication_year="bad", title="<i>Title</i>")
        normalized = _normalize_work(raw)
        self.assertEqual(normalized["doi"], "")
        self.assertEqual(normalized["title"], "Title")
        self.assertIsNone(normalized["year"])
        self.assertIsNone(normalized["citation_count"])
        self.assertTrue(normalized["references_truncated"])
        self.assertEqual(len(normalized["reference_ids"]), MAX_WORK_REFERENCES)

    def test_abstract_handles_case_positions_and_malformed_input(self):
        self.assertEqual(_abstract({"A": [0], "receiver": [1], "a": [2]}), "A receiver a")
        self.assertEqual(_abstract({"one": [0], "two": [0]}), "")
        self.assertEqual(_abstract({"far": [1000000000]}), "")
        self.assertEqual(_abstract({"gap": [2]}), "")
        self.assertLessEqual(len(_abstract({"x" * 100: list(range(500))})), 12000)

    def test_unknown_counts_stay_unknown_and_reference_sort_accepts_them(self):
        provider, _ = self.provider([
            response(work(1, [2, 3], doi=DOI, cited_by_count=None)),
            listed([work(2, cited_by_count=None), work(3, cited_by_count=2)]),
            response({"results": [], "meta": {"count": None}}), listed([]),
        ])
        result = provider.fetch(DOI)
        self.assertIsNone(result["works"][0]["citation_count"])
        self.assertIsNone(result["meta"]["citing_total"])
        self.assertTrue(result["meta"]["citing_truncated"])

    def test_default_sessions_are_isolated_per_worker_thread(self):
        provider = OpenAlexProvider(api_key="")
        barrier = threading.Barrier(2)
        clients = []

        def new_session():
            client = Mock()
            client.get.side_effect = [response(work(1, doi=DOI)), listed([])]
            clients.append(client)
            return client

        def fetch_in_thread():
            barrier.wait(timeout=3)
            return provider.fetch(DOI)

        with patch("paper_server.citation_provider.requests.Session", side_effect=new_session), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(fetch_in_thread) for _ in range(2)]
            results = [future.result(timeout=5) for future in futures]
        self.assertEqual(len(clients), 2)
        self.assertTrue(all(client.get.call_count == 2 for client in clients))
        self.assertTrue(all(result["meta"]["request_count"] == 2 for result in results))


if __name__ == "__main__":
    unittest.main()
