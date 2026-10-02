import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

from scripts.update_favorite_citations import (
    citation_result_code,
    derive_doi,
    load_local_doi_map,
    normalize_doi,
    normalize_title,
    parse_crossref_result,
    parse_ieee_result,
    parse_ieee_results,
    select_crossref_match,
    title_similarity,
)


class CitationUpdateTests(unittest.TestCase):
    def test_scheduler_result_fails_only_for_provider_errors(self):
        self.assertEqual(
            citation_result_code({"errors": 0, "quota_stopped": False}), 0
        )
        self.assertEqual(
            citation_result_code({"errors": 0, "quota_stopped": True}), 1
        )
        self.assertEqual(
            citation_result_code({"errors": 2, "quota_stopped": False}), 1
        )

    def test_normalize_doi_accepts_url_and_plain_doi(self):
        self.assertEqual(
            normalize_doi("https://doi.org/10.1364/OE.12345"),
            "10.1364/oe.12345",
        )
        self.assertEqual(normalize_doi("10.1038/s41566-025-1234"), "10.1038/s41566-025-1234")
        self.assertIsNone(normalize_doi(""))

    def test_title_normalization_removes_conference_session_prefix(self):
        self.assertEqual(
            normalize_title("7.2: A 224-Gb/s PAM-4 Transceiver"),
            normalize_title("A 224 Gb/s PAM-4 Transceiver"),
        )

    def test_title_normalization_removes_crossref_html_tags(self):
        self.assertEqual(
            normalize_title("Si<sub>3</sub>N<sub>4</sub> modulator"),
            normalize_title("Si3N4 modulator"),
        )

    def test_legacy_publisher_keys_derive_exact_dois(self):
        self.assertEqual(
            derive_doi(
                {
                    "article_number": "nphoton.2010.179",
                    "source_system": "nature",
                    "year": "2010",
                }
            ),
            "10.1038/nphoton.2010.179",
        )
        self.assertEqual(
            derive_doi({"article_number": "ncomms1030", "source_system": "nature"}),
            "10.1038/ncomms1030",
        )
        self.assertIsNone(
            derive_doi({"article_number": "ncomms-not-an-id", "source_system": "nature"})
        )
        self.assertIsNone(
            derive_doi({"article_number": "ncomms1030", "source_system": "ieee"})
        )
        self.assertEqual(
            derive_doi(
                {
                    "article_number": "oe-13-8-3129",
                    "source_system": "optica",
                    "year": "2005",
                }
            ),
            "10.1364/opex.13.003129",
        )

    def test_local_workbooks_do_not_choose_last_of_two_registered_dois(self):
        key = "oe-27-26-37910"
        same_key = "oe-23-23-30001"
        first = pd.DataFrame([
            {"Journal": "OE", "URL": f"https://opg.optica.org/abstract.cfm?URI={key}",
             "DOI": "10.1364/OE.27.037910"},
            {"Journal": "OE", "URL": f"https://opg.optica.org/abstract.cfm?URI={same_key}",
             "DOI": "https://doi.org/10.1364/OE.23.030001"},
        ])
        second = pd.DataFrame([
            {"Journal": "OE", "URL": f"https://opg.optica.org/abstract.cfm?URI={key}",
             "DOI": "10.1364/OE.379584"},
            {"Journal": "OE", "URL": f"https://opg.optica.org/abstract.cfm?URI={same_key}",
             "DOI": "10.1364/oe.23.030001"},
        ])
        with TemporaryDirectory() as directory:
            for name in ("a.xlsx", "b.xlsx"):
                (Path(directory) / name).touch()
            for frames in ([first, second], [second, first]):
                conflicts = {}
                with patch("scripts.update_favorite_citations.META_DIR", Path(directory)), \
                     patch("pandas.read_excel", side_effect=frames):
                    found = load_local_doi_map({key, same_key}, conflicts=conflicts)
                self.assertEqual(found, {same_key: "10.1364/oe.23.030001"})
                self.assertEqual(conflicts[key], ["10.1364/oe.27.037910", "10.1364/oe.379584"])

    def test_title_similarity_rejects_unrelated_papers(self):
        close = title_similarity(
            "Integrated Silicon Photonic Transmitter",
            "Integrated silicon-photonic transmitter",
        )
        far = title_similarity(
            "Integrated Silicon Photonic Transmitter",
            "Quantum sensing with trapped atoms",
        )
        self.assertGreater(close[0], 0.95)
        self.assertLess(far[0], 0.5)

    def test_parse_ieee_result_uses_exact_article_number_and_zero_default(self):
        payload = {
            "articles": [
                {"article_number": "42", "citing_paper_count": "17", "doi": "10.1109/X.42"},
                {"article_number": "43"},
            ]
        }
        result = parse_ieee_result(payload, "42")
        self.assertEqual(result.count, 17)
        self.assertEqual(result.source, "ieee")
        self.assertEqual(result.doi, "10.1109/x.42")
        self.assertEqual(parse_ieee_result(payload, "43").count, 0)
        self.assertIsNone(parse_ieee_result(payload, "44"))
        batch = parse_ieee_results(payload, {"42", "44"})
        self.assertEqual(set(batch), {"42"})

    def test_crossref_match_requires_title_year_and_source_prefix(self):
        paper = {
            "title": "Silicon photonic integrated transmitter",
            "year": "2025",
            "source_system": "optica",
            "source_name": "OE",
        }
        good = {
            "DOI": "10.1364/OE.123456",
            "title": ["Silicon-photonic integrated transmitter"],
            "published": {"date-parts": [[2025, 1, 1]]},
            "is-referenced-by-count": 9,
        }
        wrong_source = {
            **good,
            "DOI": "10.1038/s41566-025-0001",
            "is-referenced-by-count": 99,
        }
        selected = select_crossref_match(paper, [wrong_source, good])
        self.assertIs(selected, good)
        self.assertEqual(parse_crossref_result(selected).count, 9)


if __name__ == "__main__":
    unittest.main()
