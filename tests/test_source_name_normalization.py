import unittest

from scripts.import_excel_to_db import normalize_publication_source


class SourceNameNormalizationTests(unittest.TestCase):
    def test_old_mwtl_rows_are_normalized_to_mwcl(self):
        self.assertEqual(normalize_publication_source("MWTL", "2022"), "MWCL")
        self.assertEqual(normalize_publication_source("MWCL", "2010"), "MWCL")

    def test_new_mwcl_rows_are_normalized_to_mwtl(self):
        self.assertEqual(normalize_publication_source("MWCL", "2023"), "MWTL")
        self.assertEqual(normalize_publication_source("MWTL", "2026"), "MWTL")

    def test_other_sources_are_unchanged(self):
        self.assertEqual(normalize_publication_source("JLT", "2022"), "JLT")


if __name__ == "__main__":
    unittest.main()
