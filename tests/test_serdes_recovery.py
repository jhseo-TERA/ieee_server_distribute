import unittest

from scripts.recover_serdes_survey import canonical_venue, extract_marked_abstract
from scripts.reconcile_recovered_pdf_bundle import (
    source_name_from_metadata,
    source_name_from_venue,
    source_type_from_metadata,
)
from scripts.serdes_data_pipeline import fetch_serdes_papers, SERDES_LATEST_COMPLETE_RUNS_SQL
from serdes_metrics import extract_performance


class SerdesRecoveryTests(unittest.TestCase):
    def test_pdf_split_process_is_retained_only_as_review_candidate(self):
        for node in ("4 0nm", "2 8nm", "0nm"):
            result = extract_performance(f"The receiver is fabricated in the {node} CMOS technology.")
            self.assertIsNone(result["process_nm"])
            self.assertIn("process_nm", result["ambiguous_fields"])
            self.assertTrue(result["candidates"]["processes"][0]["needs_review"])
        self.assertEqual(extract_performance("The receiver uses 40nm CMOS.")["process_nm"], 40)

    def test_screened_sql_accepts_pymysql_parameter_interpolation(self):
        class FormattingCursor:
            def execute(self, query, params):
                self.query = query % params

            def fetchall(self):
                return []

        cursor = FormattingCursor()
        fetch_serdes_papers(cursor, screened_only=True)
        self.assertIn("LIKE 'all%'", cursor.query)
        query = f"SELECT 1 WHERE 1=%s AND 2 IN ({SERDES_LATEST_COMPLETE_RUNS_SQL})"
        self.assertIn("LIKE 'all%'", query % (1,))

    def test_canonical_venues(self):
        for raw, expected in (
            ("IEEE Transactions on Circuits and Systems II: Expr", "TCAS-II"),
            ("IEEE Transactions on Circuits and Systems I: Regul", "TCAS-I"),
            ("2024 IEEE Symposium on VLSI Technology and Circuits", "VLSI-Tech"),
            ("2018 IEEE Symposium on VLSI Circuits", "VLSI-Circuits"),
            ("A-SSCC", "ASSCC"),
            ("IEEE Open Journal of the Solid-State Circuits Society", "OJSSC"),
        ):
            self.assertEqual(source_name_from_venue(raw), expected)

    def test_unknown_source_is_preserved(self):
        self.assertEqual(canonical_venue({"source_name": "IEEE Archive", "doi": None}, ""), "IEEE Archive")
        self.assertEqual(canonical_venue({"source_name": "Session 5", "doi": "10.1109/ISSCC42614.2022.123"}, ""), "ISSCC")

    def test_recovered_session_subjects_use_publication_evidence(self):
        cases = (
            ("CIRC", "10.1109/esscirc.2007.4430354", "4430354", "ESSCIRC"),
            ("RFIC 2017 : RF Circuits", "10.1109/rfic.2017.7969067", "7969067", "RFIC"),
            ("SESSION 25 - High-Speed Wireline Circuits", "10.23919/vlsic.2017.8008527", "8008527", "VLSI-Circuits"),
            ("SESSION 15 - Memory Interface", None, "8008477", "VLSI-Circuits"),
        )
        for subject, doi, article_number, expected in cases:
            source_name = source_name_from_metadata(subject, doi, article_number)
            self.assertEqual(source_name, expected)
            self.assertEqual(source_type_from_metadata(source_name, subject), "conference")

    def test_explicit_abstract_only(self):
        body = "This paper reports a measured transmitter with a data rate of 112 Gb/s and an energy efficiency of 2 pJ/bit. The circuit includes a transmitter driver and clock generator fabricated in CMOS technology."
        extracted, reason = extract_marked_abstract("Title\nAbstract—" + body + "\nIndex Terms—SerDes\nI. INTRODUCTION\nOther text")
        self.assertEqual(extracted, body)
        self.assertEqual(reason, "explicit_abstract_to_heading")
        self.assertIsNone(extract_marked_abstract(body)[0])
        self.assertIsNone(extract_marked_abstract("Abstract—" + body)[0])

    def test_introduction_boundary_and_hyphenation(self):
        body = "The measured trans-\nmitter operates in a recovered clock architecture. " * 4
        abstract, _ = extract_marked_abstract("Abstract:\n" + body + "\nI. INTRODUCTION\nUnrelated measured rate 200 Gb/s")
        self.assertIn("transmitter", abstract)
        self.assertNotIn("200 Gb/s", abstract)


if __name__ == "__main__":
    unittest.main()
