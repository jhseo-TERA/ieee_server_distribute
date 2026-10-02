from types import SimpleNamespace
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import download_optica_pdfs


class FakeCursor:
    def __init__(self):
        self.sql = ""
        self.params = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params):
        self.sql = sql
        self.params = list(params)

    def fetchall(self):
        return []


class FakeConnection:
    def __init__(self):
        self.last_cursor = FakeCursor()

    def cursor(self):
        return self.last_cursor


class OpticaQueueTests(unittest.TestCase):
    def test_ieee_jlt_route_and_optica_queue_are_disjoint(self):
        for route, expected in (("jlt-ieee", "AND (source_name = 'JLT'"),
                                ("optica", "AND NOT (source_name = 'JLT'")):
            conn = FakeConnection()
            args = SimpleNamespace(route=route, source=None, min_year=None, limit=8)
            with (patch.object(download_optica_pdfs, "load_repair_ids", return_value=[]),
                  patch.object(download_optica_pdfs, "deferred_item_ids", return_value=[])):
                download_optica_pdfs.fetch_targets(conn, args)
            self.assertIn(expected, conn.last_cursor.sql)
            self.assertIn("COALESCE(url, '')", conn.last_cursor.sql)

    def test_ieee_jlt_login_does_not_open_optica(self):
        with (patch.object(download_optica_pdfs, "PROVIDER", "jlt_ieee"),
              patch.object(download_optica_pdfs, "ieee_selenium_login", return_value="driver") as login,
              patch.object(download_optica_pdfs, "refresh_proxy_session") as optica_login):
            self.assertEqual(download_optica_pdfs.selenium_login(), "driver")
            login.assert_called_once()
            optica_login.assert_not_called()

    def test_block_cooldowns_are_reason_specific(self):
        self.assertEqual(
            download_optica_pdfs.OPTICA_HEAVY_USAGE_COOLDOWN_SECONDS,
            48 * 3600,
        )
        self.assertEqual(
            download_optica_pdfs.OPTICA_CAPTCHA_COOLDOWN_SECONDS,
            72 * 3600,
        )
        self.assertGreater(
            download_optica_pdfs.OPTICA_CAPTCHA_COOLDOWN_SECONDS,
            download_optica_pdfs.OPTICA_HEAVY_USAGE_COOLDOWN_SECONDS,
        )

    def test_default_pacing_stays_below_six_logical_papers_per_hour(self):
        self.assertGreaterEqual(
            download_optica_pdfs.OPTICA_MIN_DELAY_SECONDS, 660.0
        )
        self.assertGreaterEqual(
            download_optica_pdfs.OPTICA_BATCH_PAUSE_MIN_SECONDS, 660.0
        )
        self.assertLessEqual(
            download_optica_pdfs.OPTICA_MAX_DELAY_SECONDS, 720.0
        )
        self.assertLessEqual(
            download_optica_pdfs.OPTICA_BATCH_PAUSE_MAX_SECONDS, 720.0
        )

    def test_proxy_health_check_is_due_once_after_two_hours(self):
        started = 100.0
        threshold = download_optica_pdfs.OPTICA_PROXY_REFRESH_SECONDS

        self.assertFalse(
            download_optica_pdfs.proxy_refresh_due(
                started, False, now=started + threshold - 0.01
            )
        )
        self.assertTrue(
            download_optica_pdfs.proxy_refresh_due(
                started, False, now=started + threshold
            )
        )
        self.assertFalse(
            download_optica_pdfs.proxy_refresh_due(
                started, True, now=started + threshold + 3600
            )
        )

    def test_library_host_is_not_an_optica_article_host(self):
        self.assertTrue(
            download_optica_pdfs.is_optica_article_url(
                "https://opg-optica-org-ssl.access.yonsei.ac.kr/jlt/abstract.cfm"
            )
        )
        self.assertFalse(
            download_optica_pdfs.is_optica_article_url(
                "https://library.yonsei.ac.kr/jlt/viewmedia.cfm?uri=x"
            )
        )

    def test_two_hour_probe_does_not_claim_relogin_when_session_is_still_valid(self):
        class Driver:
            current_url = "https://library.yonsei.ac.kr/"

            def get(self, _url):
                self.current_url = (
                    "https://opg-optica-org-ssl.access.yonsei.ac.kr/prj/browse.cfm"
                )

        driver = Driver()
        wait = Mock()
        wait.until.return_value = "ready"
        with (
            patch.object(download_optica_pdfs, "WebDriverWait", return_value=wait),
            patch.object(download_optica_pdfs, "close_other_windows"),
            patch.object(download_optica_pdfs, "drain_performance_log"),
        ):
            result = download_optica_pdfs.refresh_proxy_session(driver)

        self.assertEqual(result, (True, False))

    def test_expired_gateway_never_builds_library_viewmedia_fallback(self):
        class Driver:
            def __init__(self):
                self.current_url = "about:blank"
                self.visited = []

            def get(self, url):
                self.visited.append(url)
                self.current_url = "https://library.yonsei.ac.kr/"

            def get_log(self, _name):
                return []

        driver = Driver()
        with (
            patch.object(download_optica_pdfs.time, "sleep"),
            patch.object(download_optica_pdfs, "check_captcha", return_value=False),
            patch.object(download_optica_pdfs, "check_rate_limit", return_value=False),
        ):
            result = download_optica_pdfs.download_one(
                driver,
                "jlt-doi-10-1109-jlt-2025-3525646",
                "https://opg.optica.org/jlt/abstract.cfm?uri=jlt-43-8-3869",
            )

        self.assertEqual(result[0:2], ("SESSION_EXPIRED", "proxy_session_expired"))
        self.assertEqual(len(driver.visited), 1)
        self.assertNotIn("library.yonsei.ac.kr/jlt/viewmedia.cfm", driver.visited[0])

    def test_expired_session_reauthenticates_and_retries_same_logical_item(self):
        expired = (
            "SESSION_EXPIRED",
            "proxy_session_expired",
            {"final_url": "https://library.yonsei.ac.kr/"},
        )
        pdf = b"%PDF-1.7\nvalid-enough-for-this-unit-test"
        with (
            patch.object(
                download_optica_pdfs,
                "download_one",
                side_effect=[expired, pdf],
            ) as download,
            patch.object(
                download_optica_pdfs,
                "refresh_proxy_session",
                return_value=(True, True),
            ) as refresh,
            patch.object(download_optica_pdfs, "capture_current_page_diagnostic"),
        ):
            result, reauth_used, stop = (
                download_optica_pdfs.download_with_proxy_recovery(
                    object(), "paper-1", "https://opg.optica.org/abstract.cfm?uri=x",
                    allow_reauth=True,
                )
            )

        self.assertEqual(result, pdf)
        self.assertTrue(reauth_used)
        self.assertFalse(stop)
        self.assertEqual(download.call_count, 2)
        refresh.assert_called_once()

    def test_failed_reauthentication_stops_without_a_second_article_request(self):
        expired = (
            "SESSION_EXPIRED",
            "proxy_session_expired",
            {"final_url": "https://library.yonsei.ac.kr/"},
        )
        with (
            patch.object(
                download_optica_pdfs, "download_one", return_value=expired
            ) as download,
            patch.object(
                download_optica_pdfs,
                "refresh_proxy_session",
                return_value=(False, False),
            ),
            patch.object(download_optica_pdfs, "capture_current_page_diagnostic"),
        ):
            result, reauth_used, stop = (
                download_optica_pdfs.download_with_proxy_recovery(
                    object(), "paper-1", None, allow_reauth=True
                )
            )

        self.assertEqual(result[0:2], ("FAILED", "proxy_reauth_failed"))
        self.assertTrue(reauth_used)
        self.assertTrue(stop)
        download.assert_called_once()

    def test_immediate_provider_block_captures_diagnose_only_state(self):
        class Driver:
            current_url = (
                "https://opg-optica-org-ssl.access.yonsei.ac.kr/"
                "viewmedia.cfm?uri=OFC-2026-M1B.2&seq=0"
            )

        expected_path = Path("blocked-diagnostic.json")
        with patch.object(
            download_optica_pdfs,
            "capture_optica_diagnostic",
            return_value=expected_path,
        ) as capture:
            result = download_optica_pdfs.capture_current_page_diagnostic(
                Driver(),
                article_number="OFC-2026-M1B.2",
                stored_url=None,
                failure_reason="heavy_usage_timeout",
                failure_details={"cooldown_seconds": 43200},
                trigger="immediate_provider_block",
            )

        self.assertEqual(result, expected_path)
        self.assertEqual(
            capture.call_args.kwargs["trigger"], "immediate_provider_block"
        )

    def test_last_item_does_not_wait_after_download(self):
        guard = Mock()
        args = SimpleNamespace(
            batch_size=10, batch_pause_min=300.0, batch_pause_max=420.0
        )

        result = download_optica_pdfs.pace_between_items(
            guard, args, done=2, index=2, total=2
        )

        self.assertEqual(result, 0.0)
        guard.pace.assert_not_called()

    def test_wait_finds_direct_pdf_tab_without_visible_switching(self):
        direct_url = (
            "https://opg-optica-org-ssl.access.yonsei.ac.kr/"
            "directpdfaccess/token/ofc-2026-m2b.4.pdf?da=1"
        )

        class Driver:
            def __init__(self):
                self.switch_count = 0

            @property
            def current_url(self):
                return "https://proxy.example/view_article.cfm?pdfKey=x"

            def execute_cdp_cmd(self, command, _params):
                self.assert_no_switch()
                if command == "Target.getTargets":
                    return {
                        "targetInfos": [
                            {"type": "page", "url": self.current_url},
                            {"type": "page", "url": direct_url},
                        ]
                    }
                return {}

            def get_log(self, _name):
                return []

            def find_elements(self, _by, _selector):
                return []

            def assert_no_switch(self):
                if self.switch_count:
                    raise AssertionError("visible tab switch occurred")

        driver = Driver()
        final_url, candidates = download_optica_pdfs.wait_for_pdf_candidates(
            driver, timeout=0
        )

        self.assertEqual(final_url, direct_url)
        self.assertEqual(candidates, [direct_url])
        self.assertEqual(driver.switch_count, 0)

    def test_redirect_wait_allows_slow_viewer_initialization(self):
        self.assertGreaterEqual(
            download_optica_pdfs.OPTICA_PDF_REDIRECT_TIMEOUT_SECONDS, 60.0
        )

    def test_stale_tabs_are_closed_only_when_cleanup_is_requested(self):
        class SwitchTo:
            def __init__(self, driver):
                self.driver = driver

            def window(self, handle):
                self.driver.active = handle

        class Driver:
            current_window_handle = "viewer"
            window_handles = ["abstract", "viewer"]

            def __init__(self):
                self.active = "viewer"
                self.closed = []
                self.switch_to = SwitchTo(self)

            def close(self):
                self.closed.append(self.active)

        driver = Driver()
        download_optica_pdfs.close_other_windows(driver)

        self.assertEqual(driver.closed, ["abstract"])
        self.assertEqual(driver.active, "viewer")

    def test_performance_log_extracts_only_trusted_pdf_responses(self):
        def event(url, mime_type):
            return {
                "message": json.dumps(
                    {
                        "message": {
                            "method": "Network.responseReceived",
                            "params": {
                                "response": {"url": url, "mimeType": mime_type}
                            },
                        }
                    }
                )
            }

        trusted = (
            "https://opg-optica-org-ssl.access.yonsei.ac.kr/"
            "directpdfaccess/token/OFC-2026-W3E.6.pdf"
        )
        entries = [
            event("https://opg.optica.org/view_article.cfm?pdfKey=x", "text/html"),
            event("https://evil.example/paper.pdf", "application/pdf"),
            event(trusted, "application/pdf"),
        ]

        self.assertEqual(
            download_optica_pdfs.parse_pdf_response_urls(entries), [trusted]
        )

    def test_html_viewer_is_not_a_pdf_download_candidate(self):
        self.assertFalse(
            download_optica_pdfs.trusted_optica_download_url(
                "javascript:alert('no')"
            )
        )
        entries = [
            {
                "message": json.dumps(
                    {
                        "message": {
                            "method": "Network.responseReceived",
                            "params": {
                                "response": {
                                    "url": (
                                        "https://opg-optica-org-ssl.access."
                                        "yonsei.ac.kr/view_article.cfm?pdfKey=x"
                                    ),
                                    "mimeType": "text/html",
                                }
                            },
                        }
                    }
                )
            }
        ]
        self.assertEqual(download_optica_pdfs.parse_pdf_response_urls(entries), [])

    def test_recent_item_failures_are_excluded_from_next_target_batch(self):
        connection = FakeConnection()
        args = SimpleNamespace(source=None, min_year=None, limit=30)
        with (
            patch.object(download_optica_pdfs, "load_repair_ids", return_value=()),
            patch.object(
                download_optica_pdfs,
                "deferred_item_ids",
                return_value=("bad-jlt", "bad-ofc"),
            ),
        ):
            download_optica_pdfs.fetch_targets(connection, args)

        self.assertIn("article_number NOT IN (%s,%s)", connection.last_cursor.sql)
        self.assertEqual(connection.last_cursor.params[-3:], ["bad-jlt", "bad-ofc", 30])

    def test_third_failure_captures_diagnostic_without_starting_another_run(self):
        class Guard:
            def failure(self):
                return True

        class Driver:
            current_url = "https://proxy.example/viewmedia.cfm?uri=OFC-2026-M2A.2"

        retry_at = datetime(2026, 8, 11, 7, 0, tzinfo=timezone.utc)
        expected_path = Path("diagnostic.json")
        with (
            patch.object(
                download_optica_pdfs,
                "record_item_failure",
                return_value=retry_at,
            ),
            patch.object(
                download_optica_pdfs,
                "capture_optica_diagnostic",
                return_value=expected_path,
            ) as capture,
        ):
            result = download_optica_pdfs.record_failure_and_maybe_diagnose(
                Guard(),
                Driver(),
                article_number="OFC-2026-M2A.2",
                stored_url="https://opg.optica.org/abstract.cfm?URI=OFC-2026-M2A.2",
                failure_reason="redirect_stalled",
                failure_details={"status_code": 200},
            )

        self.assertEqual(result, (retry_at, True, expected_path))
        capture.assert_called_once()

    def test_non_terminal_failure_does_not_capture_diagnostic(self):
        class Guard:
            def failure(self):
                return False

        retry_at = datetime(2026, 8, 11, 7, 0, tzinfo=timezone.utc)
        with (
            patch.object(
                download_optica_pdfs,
                "record_item_failure",
                return_value=retry_at,
            ),
            patch.object(
                download_optica_pdfs, "capture_optica_diagnostic"
            ) as capture,
        ):
            result = download_optica_pdfs.record_failure_and_maybe_diagnose(
                Guard(),
                object(),
                article_number="OFC-2026-M2A.2",
                stored_url=None,
                failure_reason="request_exception",
            )

        self.assertEqual(result, (retry_at, False, None))
        capture.assert_not_called()

    def test_first_redirect_failure_captures_diagnostic_before_next_item(self):
        class Guard:
            def failure(self):
                return False

        retry_at = datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc)
        expected_path = Path("first-failure.json")
        with (
            patch.object(
                download_optica_pdfs,
                "record_item_failure",
                return_value=retry_at,
            ),
            patch.object(
                download_optica_pdfs,
                "capture_current_page_diagnostic",
                return_value=expected_path,
            ) as capture,
        ):
            result = download_optica_pdfs.record_failure_and_maybe_diagnose(
                Guard(),
                object(),
                article_number="oe-34-9-17284",
                stored_url=(
                    "https://opg.optica.org/oe/abstract.cfm"
                    "?uri=oe-34-9-17284"
                ),
                failure_reason="redirect_stalled",
                failure_details={"final_url": "https://proxy/viewmedia.cfm"},
            )

        self.assertEqual(result, (retry_at, False, expected_path))
        self.assertEqual(
            capture.call_args.kwargs["trigger"], "first_structural_failure"
        )


if __name__ == "__main__":
    unittest.main()
