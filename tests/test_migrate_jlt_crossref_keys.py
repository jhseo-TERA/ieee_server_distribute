import unittest

from scripts.migrate_jlt_crossref_keys import jlt_doi_key, normalize_title


class JltMigrationTests(unittest.TestCase):
    def test_doi_key_matches_importer_format(self):
        self.assertEqual(
            jlt_doi_key("10.1109/JLT.2026.1234567"),
            "jlt-doi-10-1109-jlt-2026-1234567",
        )

    def test_title_normalization_handles_markup_variants(self):
        self.assertEqual(
            normalize_title("A 100-Gb/s Optical Link"),
            normalize_title("A 100 Gb/s Optical Link"),
        )


if __name__ == "__main__":
    unittest.main()
