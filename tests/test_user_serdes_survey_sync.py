import tempfile
from pathlib import Path
import unittest

import openpyxl

from scripts.sync_user_serdes_survey import (
    fom_consistency,
    load_reference_rows,
    positive_number,
    process_value,
    venue_aliases,
)


class UserSerdesSurveySyncTests(unittest.TestCase):
    def test_positive_number_preserves_scope_and_rejects_bounds(self):
        scoped = positive_number("465.9 (front-end only)")
        bounded = positive_number("<1")
        zero = positive_number(0)

        self.assertEqual(scoped.value, 465.9)
        self.assertEqual(scoped.qualifier, "(front-end only)")
        self.assertIsNone(bounded.value)
        self.assertTrue(bounded.uncertain)
        self.assertIsNone(zero.value)

    def test_process_units_are_normalized_without_losing_raw_text(self):
        self.assertEqual(process_value(0.13)[1], 130.0)
        self.assertEqual(process_value("18A")[1], 1.8)
        self.assertEqual(process_value("180,65")[1], 65.0)
        self.assertIsNone(process_value("1z")[1])

    def test_fom_consistency_uses_mw_over_gbps_identity(self):
        self.assertEqual(fom_consistency(112, 241.92, 2.16)["status"], "consistent")
        self.assertEqual(fom_consistency(40, 32, 0.4)["status"], "conflict")

    def test_loader_ignores_formatting_legend_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "survey.xlsx"
            workbook = openpyxl.Workbook()
            worksheet = workbook.active
            worksheet.title = "SurveyData - ISSCC"
            worksheet.append([
                "Year", "Publication", "Title", "First Author", "Affiliation",
                "Process", "Loss (dB)", "Loss Frequency (GHz)", "Speed (Gb/s)",
                "Power(mW)", "Energy/Bit [pJ]", "Main Category", "Sub Category", "Status",
            ])
            worksheet.append([
                2024, "ISSCC", "A 112-Gb/s SerDes", "A. Author", "Lab", 28,
                31, 28, 112, 241.92, 2.16, "Electrical",
            ])
            worksheet.cell(row=100, column=14, value="legend only")
            workbook.save(path)

            rows = load_reference_rows(path)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "A 112-Gb/s SerDes")
        self.assertEqual(rows[0]["fom_check"]["status"], "consistent")

    def test_venue_aliases_cover_sheet_naming_variants(self):
        self.assertEqual(venue_aliases("ESSCIRC", "SurveyData - ESSERC"), ("ESSCIRC",))
        self.assertEqual(
            venue_aliases("VLSI", "SurveyData - VLSI"),
            ("VLSI-CIRCUITS", "VLSI-TECH"),
        )


if __name__ == "__main__":
    unittest.main()
