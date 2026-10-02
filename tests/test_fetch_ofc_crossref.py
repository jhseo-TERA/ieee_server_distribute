import unittest

from scripts.fetch_ofc_crossref import (
    canonical_url,
    format_authors,
    item_to_row,
    ofc_doi_match,
    year_range,
)


class OfcCrossrefTests(unittest.TestCase):
    def test_strict_doi_filter_rejects_other_optica_conferences(self):
        self.assertTrue(ofc_doi_match("10.1364/OFC.2025.W1K.2", 2025))
        self.assertFalse(ofc_doi_match("10.1364/CLEO_SI.2025.STh1A.1", 2025))
        self.assertFalse(ofc_doi_match("10.1364/OFC.2024.W1K.2", 2025))

    def test_item_maps_to_import_compatible_conference_row(self):
        item = {
            "DOI": "10.1364/ofc.2025.w1k.2",
            "title": ["Predicting Nonlinear Interference"],
            "author": [
                {"given": "Jingxin", "family": "Deng"},
                {"given": "Bin", "family": "Chen"},
            ],
            "published": {"date-parts": [[2025]]},
            "resource": {
                "primary": {
                    "URL": "https://opg.optica.org/abstract.cfm?URI=OFC-2025-W1K.2"
                }
            },
        }

        row = item_to_row(item, 2025)

        self.assertEqual(row["Conference"], "OFC")
        self.assertEqual(row["Year"], "2025")
        self.assertEqual(row["Page"], "w1k.2")
        self.assertEqual(row["Authors"], "Jingxin Deng and Bin Chen")
        self.assertIn("URI=OFC-2025-W1K.2", row["URL"])

    def test_url_falls_back_to_doi_components(self):
        item = {"DOI": "10.1364/OFC.2026.Th4B.4"}
        self.assertEqual(
            canonical_url(item, 2026),
            "https://opg.optica.org/abstract.cfm?URI=OFC-2026-Th4B.4",
        )

    def test_title_decodes_entities_and_removes_markup(self):
        item = {
            "DOI": "10.1364/OFC.2026.M2A.2",
            "title": ["Bandwidth &gt;400 <i>Gb/s</i>"],
            "published": {"date-parts": [[2026]]},
        }

        row = item_to_row(item, 2026)

        self.assertEqual(row["Title"], "Bandwidth >400 Gb/s")

    def test_author_format_and_descending_year_range(self):
        authors = [
            {"given": "A", "family": "One"},
            {"given": "B", "family": "Two"},
            {"given": "C", "family": "Three"},
        ]
        self.assertEqual(format_authors(authors), "A One, B Two, and C Three")
        self.assertEqual(list(year_range(2024, 2026)), [2026, 2025, 2024])


if __name__ == "__main__":
    unittest.main()
