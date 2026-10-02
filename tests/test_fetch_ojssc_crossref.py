import unittest

from scripts.fetch_ojssc_crossref import (
    extract_ieee_url,
    extract_issue,
    format_authors,
    item_to_row,
)


class OjsscCrossrefTests(unittest.TestCase):
    def test_item_maps_to_import_compatible_journal_row(self):
        item = {
            "DOI": "10.1109/OJSSCS.2025.1234567",
            "title": ["An &gt;80-Gb/s <i>Receiver</i>"],
            "author": [
                {"given": "A", "family": "One"},
                {"given": "B", "family": "Two"},
            ],
            "published": {"date-parts": [[2025]]},
            "volume": "5",
            "page": "100-112",
            "resource": {
                "primary": {"URL": "https://ieeexplore.ieee.org/document/1234567/"}
            },
        }

        row = item_to_row(item)

        self.assertEqual(row["Journal"], "OJSSC")
        self.assertEqual(row["Year"], "2025")
        self.assertEqual(row["Title"], "An >80-Gb/s Receiver")
        self.assertEqual(row["Authors"], "A One and B Two")
        self.assertEqual(row["Issue"], "Vol. 5, pp. 100-112")
        self.assertEqual(row["URL"], "https://ieeexplore.ieee.org/document/1234567/")

    def test_authorless_front_matter_is_excluded(self):
        item = {
            "DOI": "10.1109/OJSSCS.2025.7654321",
            "title": ["Table of Contents"],
            "published": {"date-parts": [[2025]]},
            "resource": {
                "primary": {"URL": "https://ieeexplore.ieee.org/document/7654321/"}
            },
        }
        self.assertIsNone(item_to_row(item))

    def test_non_ieee_document_url_is_excluded(self):
        item = {
            "DOI": "10.1109/OJSSCS.2025.1111111",
            "title": ["A Paper"],
            "author": [{"given": "A", "family": "One"}],
            "published": {"date-parts": [[2025]]},
            "resource": {"primary": {"URL": "https://doi.org/10.1109/OJSSCS.2025.1111111"}},
        }
        self.assertIsNone(extract_ieee_url(item))
        self.assertIsNone(item_to_row(item))

    def test_author_format_and_issue_fallbacks(self):
        self.assertEqual(
            format_authors(
                [
                    {"given": "A", "family": "One"},
                    {"given": "B", "family": "Two"},
                    {"given": "C", "family": "Three"},
                ]
            ),
            "A One, B Two, and C Three",
        )
        self.assertEqual(extract_issue({"volume": "4"}), "Vol. 4")
        self.assertIsNone(extract_issue({}))


if __name__ == "__main__":
    unittest.main()
