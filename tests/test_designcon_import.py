import unittest
from pathlib import Path

from scripts.import_designcon_to_db import (
    document_kind,
    issue_from_path,
    title_from_filename,
)


class DesignConImportTests(unittest.TestCase):
    def test_title_from_recent_filename(self):
        self.assertEqual(
            title_from_filename(
                "DCON26_PAPER_Track07_CrosstalkSensitivityNewFinding_256_151.pdf"
            ),
            "Crosstalk Sensitivity New Finding",
        )

    def test_title_from_legacy_session_filename(self):
        self.assertEqual(
            title_from_filename(
                "1-WA1_Paper_Power_Ground_bump_Optimization_Techninique.pdf"
            ),
            "Power Ground bump Optimization Techninique",
        )

    def test_repeated_download_filename_uses_original_name(self):
        self.assertEqual(
            title_from_filename(
                "SR_Modeling_DesignCon_Syong_07142021_v1.pdf_109742224_"
                "SR_Modeling_DesignCon_Syong_07142021_v1.pdf"
            ),
            "SR Modeling Design Con Syong 07142021 v1",
        )

    def test_normalized_filename_preserves_title_punctuation(self):
        self.assertEqual(
            title_from_filename(
                "Track 07 - Paper - PCIe 7.0 Channel and S-Parameter Analysis.pdf"
            ),
            "PCIe 7.0 Channel and S-Parameter Analysis",
        )

    def test_document_kind_and_issue(self):
        path = Path(
            "DesignCon/DesignCon2026/Documents/Track 07/"
            "DCON26_SLIDES_Track07_Example.pdf"
        )
        self.assertEqual(document_kind(path.name), "Slides")
        self.assertEqual(issue_from_path(path, "Slides"), "Track 07 / Slides")


if __name__ == "__main__":
    unittest.main()
