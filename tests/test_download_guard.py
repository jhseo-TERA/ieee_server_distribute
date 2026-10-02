import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter

from scripts import download_guard


class DownloadGuardPolicyTests(unittest.TestCase):
    def test_limit_accepts_only_one_through_thirty(self):
        self.assertEqual(download_guard.bounded_download_limit("1"), 1)
        self.assertEqual(download_guard.bounded_download_limit("30"), 30)
        with self.assertRaises(Exception):
            download_guard.bounded_download_limit("0")
        with self.assertRaises(Exception):
            download_guard.bounded_download_limit("31")

    def test_failure_is_visible_to_scheduler(self):
        self.assertEqual(download_guard.download_result_code(30, 0), 0)
        self.assertEqual(download_guard.download_result_code(29, 1), 1)
        self.assertEqual(
            download_guard.download_result_code(0, 0, interrupted=True), 130
        )

    def test_provider_requires_daily_routine_environment_and_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            with patch.object(download_guard, "STATE_DIR", state_dir):
                with patch.dict(os.environ, {"PDF_ROUTINE_CHILD": "1"}, clear=False):
                    self.assertFalse(download_guard.invoked_by_daily_routine())
                    (state_dir / "routine.lock").write_text("test", encoding="utf-8")
                    self.assertTrue(download_guard.invoked_by_daily_routine())

    def test_repair_queue_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            queue_path = state_dir / "repair_queue.json"
            with (
                patch.object(download_guard, "STATE_DIR", state_dir),
                patch.object(download_guard, "REPAIR_QUEUE_PATH", queue_path),
            ):
                download_guard.queue_repairs(
                    [
                        {
                            "provider": "ieee",
                            "article_number": "8413082",
                            "reason": "test",
                        }
                    ]
                )
                self.assertEqual(
                    download_guard.load_repair_ids("ieee"), ("8413082",)
                )
                download_guard.mark_repair_complete("ieee", "8413082")
                self.assertEqual(download_guard.load_repair_ids("ieee"), ())

    def test_downloaded_pdf_requires_parseable_page_structure(self):
        with tempfile.TemporaryDirectory() as directory:
            valid_path = Path(directory) / "valid.pdf"
            invalid_path = Path(directory) / "invalid.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            with valid_path.open("wb") as handle:
                writer.write(handle)
            invalid_path.write_bytes(b"%PDF-1.7\ntruncated")

            self.assertEqual(
                download_guard.validate_pdf_file(valid_path), (True, None)
            )
            valid, error = download_guard.validate_pdf_file(invalid_path)
            self.assertFalse(valid)
            self.assertTrue(error)

    def test_item_failure_backoff_defers_then_releases_and_escalates(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            start = datetime(2026, 8, 10, 7, 0, tzinfo=timezone.utc)
            with patch.object(download_guard, "STATE_DIR", state_dir):
                first_retry = download_guard.record_item_failure(
                    "optica", "bad-item", "access_or_format", now=start
                )
                self.assertEqual(first_retry, start + timedelta(days=1))
                self.assertEqual(
                    download_guard.deferred_item_ids("optica", now=start),
                    ("bad-item",),
                )
                self.assertEqual(
                    download_guard.deferred_item_ids(
                        "optica", now=start + timedelta(days=1, seconds=1)
                    ),
                    (),
                )

                second_retry = download_guard.record_item_failure(
                    "optica",
                    "bad-item",
                    "access_or_format",
                    now=start + timedelta(days=2),
                )
                self.assertEqual(second_retry, start + timedelta(days=5))
                download_guard.clear_item_failure("optica", "bad-item")
                self.assertEqual(
                    download_guard.deferred_item_ids("optica", now=start), ()
                )


if __name__ == "__main__":
    unittest.main()
