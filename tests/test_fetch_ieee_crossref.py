import unittest

from scripts.fetch_ieee_crossref import (
    JOURNALS,
    cicc_doi_match,
    extract_ieee_url,
    icta_container_match,
    icta_doi_match,
    item_to_cicc_row,
    item_to_icta_row,
    item_to_journal_row,
    retry_after_seconds,
)


def sample_item(**overrides):
    item = {
        "DOI": "10.1109/LSSC.2025.1234567",
        "title": ["A &gt;100-GHz <i>Receiver</i>"],
        "author": [
            {"given": "A", "family": "One"},
            {"given": "B", "family": "Two"},
        ],
        "published": {"date-parts": [[2025]]},
        "volume": "8",
        "issue": "4",
        "page": "100-103",
        "resource": {
            "primary": {"URL": "https://ieeexplore.ieee.org/document/1234567/"}
        },
    }
    item.update(overrides)
    return item


class IeeeCrossrefTests(unittest.TestCase):
    def test_journal_item_maps_to_import_format(self):
        row = item_to_journal_row(sample_item(), "SSCL")
        self.assertEqual(row["Journal"], "SSCL")
        self.assertEqual(row["Year"], "2025")
        self.assertEqual(row["Title"], "A >100-GHz Receiver")
        self.assertEqual(row["Authors"], "A One and B Two")
        self.assertEqual(row["Issue"], "Vol. 8, Issue 4, pp. 100-103")

    def test_authorless_and_deleted_doi_records_are_excluded(self):
        self.assertIsNone(item_to_journal_row(sample_item(author=[]), "PTL"))
        deleted = sample_item(
            resource={"primary": {"URL": "http://www.crossref.org/deleted_DOI.html"}}
        )
        self.assertIsNone(extract_ieee_url(deleted))
        self.assertIsNone(item_to_journal_row(deleted, "PTL"))

    def test_ieee_document_url_is_normalized_to_https(self):
        item = sample_item(
            resource={
                "primary": {"URL": "http://ieeexplore.ieee.org/document/1234567/"}
            }
        )
        self.assertEqual(
            extract_ieee_url(item),
            "https://ieeexplore.ieee.org/document/1234567/",
        )

    def test_cicc_doi_pattern_accepts_old_and_new_identifiers(self):
        self.assertTrue(cicc_doi_match("10.1109/CICC.2015.7338429", 2015))
        self.assertTrue(cicc_doi_match("10.1109/CICC63670.2025.10983244", 2025))
        self.assertFalse(cicc_doi_match("10.1109/ISSCC49663.2026.11409086", 2026))

    def test_cicc_item_maps_to_conference_format(self):
        item = sample_item(
            DOI="10.1109/CICC63670.2025.10983244",
            **{"container-title": ["2025 IEEE Custom Integrated Circuits Conference (CICC)"]},
        )
        row = item_to_cicc_row(item, 2025)
        self.assertEqual(row["Conference"], "CICC")
        self.assertEqual(row["Year"], "2025")
        self.assertEqual(row["Page"], "100-103")

    def test_icta_doi_pattern_accepts_2018_and_later_identifiers(self):
        self.assertTrue(icta_doi_match("10.1109/CICTA.2018.8705949", 2018))
        self.assertTrue(icta_doi_match("10.1109/ICTA68203.2025.11329834", 2025))
        self.assertFalse(icta_doi_match("10.1109/ICTA68203.2025.11329834", 2024))
        self.assertFalse(icta_doi_match("10.1109/ICTAI.2025.1234567", 2025))

    def test_icta_item_maps_to_conference_format(self):
        item = sample_item(
            DOI="10.1109/ICTA68203.2025.11329834",
            **{
                "container-title": [
                    "2025 IEEE International Conference on Integrated Circuits, "
                    "Technologies and Applications (ICTA)"
                ]
            },
        )
        self.assertTrue(icta_container_match(item))
        row = item_to_icta_row(item, 2025)
        self.assertEqual(row["Conference"], "ICTA")
        self.assertEqual(row["Year"], "2025")
        self.assertEqual(row["Page"], "100-103")

    def test_retry_after_numeric_value(self):
        self.assertEqual(retry_after_seconds("7", 2), 7.0)
        self.assertEqual(retry_after_seconds(None, 4), 4.0)

    def test_microwave_letters_use_separate_crossref_eras(self):
        self.assertEqual(JOURNALS["MWCL"]["full_end"], 2022)
        self.assertEqual(JOURNALS["MWTL"]["full_start"], 2023)
        self.assertNotEqual(JOURNALS["MWCL"]["issn"], JOURNALS["MWTL"]["issn"])

    def test_ssc_magazine_uses_complete_print_issn(self):
        self.assertEqual(JOURNALS["SSC-M"]["issn"], "1943-0582")
        self.assertEqual(JOURNALS["SSC-M"]["full_start"], 2009)


if __name__ == "__main__":
    unittest.main()
