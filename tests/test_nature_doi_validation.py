import unittest

from scripts.validate_nature_doi_candidates import validate_candidate


class NatureDoiValidationTests(unittest.TestCase):
    def setUp(self):
        self.candidate = {
            "article_number": "ncomms1030", "proposed_doi": "10.1038/ncomms1030",
            "source_name": "NCOMMS", "title": "An α-wave Si3N4 device",
        }
        self.record = {
            "DOI": "10.1038/ncomms1030", "container-title": ["Nature Communications"],
            "title": ["An α wave Si<sub>3</sub>N<sub>4</sub> device"],
        }

    def test_exact_doi_title_and_source_are_required(self):
        self.assertEqual(validate_candidate(self.candidate, [self.record])["status"], "verified")
        for field, value, expected in (
            ("DOI", "10.1038/ncomms1031", "not_found"),
            ("title", ["An β-wave Si3N4 device"], "title_mismatch"),
            ("container-title", ["Nature Photonics"], "source_mismatch"),
        ):
            self.assertEqual(validate_candidate(self.candidate, [{**self.record, field: value}])["status"], expected)

    def test_missing_or_duplicate_records_are_not_verified(self):
        self.assertEqual(validate_candidate(self.candidate, [])["status"], "not_found")
        self.assertEqual(validate_candidate(self.candidate, [self.record, self.record])["status"], "multiple_records")


if __name__ == "__main__":
    unittest.main()
