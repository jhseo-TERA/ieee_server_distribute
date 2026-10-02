import os
import tempfile
import unittest
from unittest.mock import patch

import web.app as app_module


class DesignConPdfPathTests(unittest.TestCase):
    def test_nested_designcon_path_is_allowed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root = os.path.join(temp_dir, "DesignCon")
            relative = os.path.join(
                "DesignCon",
                "DesignCon2026",
                "Documents",
                "Track 07",
                "example.pdf",
            )
            expected = os.path.realpath(os.path.join(temp_dir, relative))
            with (
                patch.object(app_module, "ROOT", temp_dir),
                patch.dict(
                    app_module.PDF_DIR_BY_PUB,
                    {"designcon": source_root},
                    clear=True,
                ),
            ):
                actual = app_module._resolve_pdf_path(
                    "designcon", "designcon-2026-example", relative
                )
            self.assertEqual(actual, expected)

    def test_path_outside_designcon_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root = os.path.join(temp_dir, "DesignCon")
            traversal = os.path.join("DesignCon", "..", "private.pdf")
            with (
                patch.object(app_module, "ROOT", temp_dir),
                patch.dict(
                    app_module.PDF_DIR_BY_PUB,
                    {"designcon": source_root},
                    clear=True,
                ),
            ):
                actual = app_module._resolve_pdf_path(
                    "designcon", "designcon-2026-example", traversal
                )
            self.assertIsNone(actual)


if __name__ == "__main__":
    unittest.main()
