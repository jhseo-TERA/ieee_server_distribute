"""Downloader failures must retain counters used by the daily routine."""
import io
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import Mock, mock_open, patch

from scripts import download_nature_pdfs as nature
from scripts import download_pdfs as ieee


class DownloadOutcomeTests(unittest.TestCase):
    def run_downloader(self, module, *, relogin_failure=None, quit_failure=False):
        driver = Mock()
        driver.current_url = "https://www-nature-com-ssl.access.yonsei.ac.kr/nphoton/"
        if quit_failure:
            driver.quit.side_effect = RuntimeError("sensitive-request-token")
        connection = Mock()
        guard = Mock()
        guard.acquire.return_value = (True, None)
        session = Mock()
        session_result = (session, "ieeexplore-ieee-org-ssl.access.yonsei.ac.kr")
        pdf = b"%PDF-1.7 mocked PDF bytes"
        if module is ieee:
            targets = [("123", "https://ieeexplore.ieee.org/document/123/", "PTL", "2026", "First")]
            if relogin_failure:
                targets.append(("456", "https://ieeexplore.ieee.org/document/456/", "PTL", "2026", "Second"))
        else:
            targets = [("s41566-test", "NPHOTON", "2026", "First")]
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.object(module.sys, "argv", ["downloader", "--limit", "2"]))
            stack.enter_context(patch.object(module, "invoked_by_daily_routine", return_value=True))
            stack.enter_context(patch.object(module, "db_connect", return_value=connection))
            stack.enter_context(patch.object(module, "fetch_targets", return_value=targets))
            stack.enter_context(patch.object(module, "DownloadGuard", return_value=guard))
            stack.enter_context(patch.object(module, "selenium_login", return_value=driver))
            stack.enter_context(patch.object(module, "validate_pdf_file", return_value=(True, None)))
            stack.enter_context(patch.object(module, "mark_repair_complete"))
            stack.enter_context(patch.object(module.os.path, "isfile", return_value=False))
            stack.enter_context(patch.object(module.os, "replace"))
            stack.enter_context(patch("builtins.open", mock_open()))
            download = stack.enter_context(patch.object(module, "download_one", side_effect=[pdf, "RELOGIN"] if relogin_failure else [pdf]))
            if module is ieee:
                authenticate = stack.enter_context(patch.object(module, "authenticate"))
                build_session = stack.enter_context(patch.object(module, "build_session", return_value=session_result))
                if relogin_failure == "authenticate":
                    authenticate.side_effect = RuntimeError("sensitive-request-token")
                elif relogin_failure == "cookies":
                    build_session.side_effect = [session_result, RuntimeError("sensitive-request-token")]
            else:
                stack.enter_context(patch.object(module, "transplant_cookies", return_value=session))
            with redirect_stdout(output):
                result = module.main()
        return result, output.getvalue(), guard, download, connection

    def test_ieee_reauthentication_failure_keeps_prior_success_and_stops(self):
        for stage in ("authenticate", "cookies"):
            with self.subTest(stage=stage):
                result, output, guard, download, connection = self.run_downloader(
                    ieee, relogin_failure=stage
                )
                self.assertEqual(result, 1)
                self.assertIn("[완료] 성공 1 / 건너뜀 0 / 실패 1", output)
                self.assertNotIn("sensitive-request-token", output)
                self.assertEqual(download.call_count, 2)
                connection.commit.assert_called_once()
                guard.failure.assert_called_once()
                guard.close.assert_called_once()

    def test_browser_quit_failure_does_not_hide_verified_success(self):
        for provider in (ieee, nature):
            with self.subTest(provider=provider.__name__):
                result, output, guard, download, connection = self.run_downloader(
                    provider, quit_failure=True
                )
                self.assertEqual(result, 0)
                self.assertIn("[완료] 성공 1 / 건너뜀 0 / 실패 0", output)
                self.assertNotIn("sensitive-request-token", output)
                download.assert_called_once()
                connection.commit.assert_called_once()
                guard.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
