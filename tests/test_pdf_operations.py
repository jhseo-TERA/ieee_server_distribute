import os
import io
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from pypdf import PdfWriter
from scripts import pdf_proxy_auth as auth
from scripts import scan_pdf_integrity as scan
from scripts import quarantine_jlt_duplicates as cleanup
from scripts import run_pdf_postprocess as post
from scripts import run_pdf_download_routine as routine


class ProxyAuthTests(unittest.TestCase):
    def test_successful_login_fills_visible_form_and_verifies_publisher(self):
        driver, form, username, password, submit = (Mock() for _ in range(5))
        driver.title = "Login"
        driver.current_url = "https://library.yonsei.ac.kr/login"
        password.is_displayed.return_value = password.is_enabled.return_value = True
        username.is_displayed.return_value = username.is_enabled.return_value = True
        submit.is_displayed.return_value = submit.is_enabled.return_value = True
        password.find_element.return_value = form
        form.find_elements.side_effect = lambda by, selector: [username] if "username" in selector else [submit]
        driver.find_elements.side_effect = lambda by, selector: [password] if "library" in driver.current_url else []
        def clicked():
            driver.current_url = "https://www-nature-com-ssl.access.yonsei.ac.kr/nphoton/"
        submit.click.side_effect = clicked
        wait = Mock()
        wait.until.side_effect = lambda predicate: predicate(driver)
        with patch.dict(os.environ, {"YONSEI_ID": "test-id", "YONSEI_PW": "test-pw"}), patch.object(auth, "WebDriverWait", return_value=wait):
            host = auth.authenticate(driver, "nature", "https://access.yonsei.ac.kr/")
        self.assertEqual(host, "www-nature-com-ssl.access.yonsei.ac.kr")
        username.send_keys.assert_called_once_with("test-id")
        password.send_keys.assert_called_once_with("test-pw")
        submit.click.assert_called_once()

    def test_missing_credentials_fail_without_disclosing_values(self):
        with patch.dict(os.environ, {"YONSEI_ID": "", "YONSEI_PW": "secret"}):
            with self.assertRaises(auth.ProxyLoginError) as caught:
                auth.require_credentials()
        self.assertIn("YONSEI_ID", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))

    def test_login_page_and_lookalike_host_are_not_authenticated(self):
        driver = Mock()
        driver.find_elements.return_value = []
        for url in ("https://library.yonsei.ac.kr/login",
                    "https://ieeexplore.ieee.org.attacker.invalid/",
                    "https://attacker.invalid/?next=ieeexplore.ieee.org"):
            driver.current_url = url
            self.assertFalse(auth.publisher_ready(driver, "ieee"))
        driver.current_url = "https://ieeexplore-ieee-org-ssl.access.yonsei.ac.kr/Xplore/home.jsp"
        self.assertTrue(auth.publisher_ready(driver, "ieee"))
        password = Mock()
        password.is_displayed.return_value = True
        password.is_enabled.return_value = True
        driver.find_elements.return_value = [password]
        self.assertFalse(auth.publisher_ready(driver, "ieee"))

    def test_hidden_login_inputs_are_not_selected(self):
        hidden, shown = Mock(), Mock()
        hidden.is_displayed.return_value = False
        shown.is_displayed.return_value = shown.is_enabled.return_value = True
        driver = Mock()
        driver.find_elements.return_value = [hidden, shown]
        self.assertEqual(auth.visible_elements(driver, "#id"), [shown])

    def test_authentication_timeout_captures_evidence_and_blocks_download(self):
        driver = Mock()
        with patch.object(auth, "WebDriverWait") as wait, patch.object(auth, "capture_failure", return_value="evidence.json") as capture:
            wait.return_value.until.side_effect = TimeoutError("private redirect token")
            with self.assertRaises(auth.ProxyLoginError) as caught:
                auth.authenticate(driver, "nature", "https://access.yonsei.ac.kr/")
        self.assertIn("evidence.json", str(caught.exception))
        self.assertNotIn("private redirect", str(caught.exception))
        capture.assert_called_once()


class PostprocessTests(unittest.TestCase):
    def test_zero_missing_or_invalid_download_count_never_runs_external_stages(self):
        for count in (0, None, "1", -1, True):
            state = {"stages": {"download": {"status": "completed", "successful_downloads": count}}}
            for resume in (False, True):
                with patch.object(post, "load_pipeline", return_value=state), patch.object(post, "run_stage") as execute:
                    self.assertEqual(post.run_postprocess(resume=resume), 0)
                    execute.assert_not_called()

    def test_zero_results_are_deferred_even_when_child_returned_zero(self):
        self.assertEqual(routine.classify_outcome(0, ["optica"], 2, 0), "deferred")
        self.assertEqual(routine.classify_outcome(0, [], 0, 0), "idle")

    def test_bootstrap_error_releases_reserved_attempts(self):
        self.assertEqual(routine.actual_attempt_count({"successes": 0, "skipped": 0, "failures": 0}, 13), 0)
        self.assertEqual(routine.classify_outcome(2, ["ieee"], 1, 0), "failed")


class InventoryTests(unittest.TestCase):
    def test_apply_requires_external_reference_verification_before_io(self):
        with patch("sys.argv", ["quarantine_jlt_duplicates.py", "--report", "unused.json", "--apply"]), \
                patch("sys.stderr", new_callable=io.StringIO), \
                patch.object(Path, "read_text") as read, \
                patch.object(cleanup.pymysql, "connect") as connect:
            with self.assertRaises(SystemExit) as caught:
                cleanup.main()
        self.assertEqual(caught.exception.code, 2)
        read.assert_not_called()
        connect.assert_not_called()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(scan, "ROOT", self.root))
        self.dirs = {p: self.root / folder for p, folder in {
            "ieee": "ieee-pdf", "optica": "optica-pdf", "nature": "nature-pdf", "designcon": "DesignCon"}.items()}
        self.stack.enter_context(patch.object(scan, "PROVIDER_DIRS", self.dirs))
        self.stack.enter_context(patch.object(scan, "QUARANTINE_DIR", self.root / "pdf-quarantine"))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def pdf(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        with path.open("wb") as handle:
            writer.write(handle)
        return path

    def record(self, key, provider, path):
        return {"article_number": key, "provider": provider, "is_favorite": 1,
                "pdf_available": 1, "pdf_local_path": path}

    def test_designcon_uses_saved_path_not_filename_stem(self):
        path = "DesignCon/2025/Documents/Long Paper Name.pdf"
        self.pdf(path)
        result = scan.inspect_files([self.record("designcon-hash", "designcon", path)])
        self.assertEqual(result["orphans"], [])
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["manual_review"], [])

    def test_designcon_invalid_or_missing_files_require_manual_action_only(self):
        path = self.root / "DesignCon/broken.pdf"
        path.parent.mkdir()
        path.write_bytes(b"not a PDF")
        records = [self.record("broken", "designcon", "DesignCon/broken.pdf"),
                   self.record("missing", "designcon", "DesignCon/missing.pdf")]
        result = scan.inspect_files(records)
        self.assertEqual(len(result["manual_review"]), 2)
        conn = Mock()
        with patch.object(scan, "queue_repairs") as queue:
            self.assertEqual(scan.apply_repairs(conn, result, "test"), 0)
            queue.assert_not_called()
            conn.cursor.assert_not_called()
        self.assertTrue(path.exists())

    def test_unreferenced_identical_jlt_is_reported_and_recoverably_moved(self):
        canonical = self.pdf("optica-pdf/jlt-doi-10-1109-jlt-2025-123.pdf")
        alias = self.root / "optica-pdf/jlt-43-10-100.pdf"
        alias.write_bytes(canonical.read_bytes())
        records = [self.record("key", "optica", canonical.relative_to(self.root).as_posix())]
        report = scan.inspect_files(records)
        self.assertEqual(len(report["orphans"]), 1)
        self.assertEqual(len(report["duplicates"]), 1)
        destination = self.root / "pdf-quarantine/duplicates/test"
        moved = cleanup.move_duplicates(report, {scan.path_key(canonical)}, self.root, destination)
        self.assertEqual(len(moved), 1)
        self.assertFalse(alias.exists())
        self.assertTrue(canonical.exists())
        self.assertEqual((destination / alias.name).read_bytes(), canonical.read_bytes())

    def test_referenced_alias_is_never_moved(self):
        canonical = self.pdf("optica-pdf/jlt-doi-10-1109-jlt-2025-123.pdf")
        alias = self.root / "optica-pdf/jlt-43-10-100.pdf"
        alias.write_bytes(canonical.read_bytes())
        records = [self.record("key", "optica", canonical.relative_to(self.root).as_posix())]
        report = scan.inspect_files(records)
        moved = cleanup.move_duplicates(report, {scan.path_key(canonical), scan.path_key(alias)}, self.root,
                                        self.root / "pdf-quarantine/test")
        self.assertEqual(moved, [])
        self.assertTrue(alias.exists())

    def test_cleanup_refuses_outside_workspace(self):
        with self.assertRaises(ValueError):
            cleanup.move_duplicates({"duplicates": []}, set(), self.root, self.root.parent / "elsewhere")

    def test_path_traversal_is_not_used_as_a_pdf_path(self):
        self.assertIsNone(scan.safe_database_path("../outside.pdf"))


if __name__ == "__main__":
    unittest.main()
