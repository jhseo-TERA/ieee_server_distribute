import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from scripts import optica_diagnostics


class FakeElement:
    def __init__(self, text, href):
        self.text = text
        self.href = href

    def get_attribute(self, name):
        return self.href if name == "href" else None


class FakeBody:
    text = "Institutional access is required."


class FakeDriver:
    title = "OFC article"
    current_url = (
        "https://opg-optica-org-ssl.access.yonsei.ac.kr/abstract.cfm"
        "?URI=OFC-2026-M2A.2&session=private"
    )

    def __init__(self):
        self.navigation_calls = 0

    def get(self, _url):
        self.navigation_calls += 1
        raise AssertionError("diagnostic capture must not navigate")

    def find_elements(self, _by, _selector):
        return [
            FakeElement(
                "Get PDF",
                "https://opg.optica.org/viewmedia.cfm"
                "?uri=OFC-2026-M2A.2&seq=0&token=secret-token",
            ),
            FakeElement("References", "https://example.invalid/references"),
        ]

    def find_element(self, _by, _selector):
        return FakeBody()


class OpticaDiagnosticTests(unittest.TestCase):
    def test_capture_is_local_only_and_redacts_sensitive_query_values(self):
        driver = FakeDriver()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                optica_diagnostics, "DIAGNOSTIC_DIR", Path(directory)
            ):
                path = optica_diagnostics.capture_optica_diagnostic(
                    driver,
                    article_number="OFC-2026-M2A.2",
                    stored_url=(
                        "https://opg.optica.org/abstract.cfm"
                        "?URI=OFC-2026-M2A.2"
                    ),
                    resolved_url=(
                        "https://opg.optica.org/abstract.cfm"
                        "?URI=OFC-2026-M2A.2"
                    ),
                    fallback_url=(
                        "https://opg.optica.org/viewmedia.cfm"
                        "?uri=OFC-2026-M2A.2&seq=0"
                    ),
                    failure_reason="redirect_stalled",
                    failure_details={"final_url": FakeDriver.current_url},
                )
                raw = path.read_text(encoding="utf-8")
                payload = json.loads(raw)

        self.assertEqual(driver.navigation_calls, 0)
        self.assertNotIn("secret-token", raw)
        self.assertNotIn("private", raw)
        safety = payload["safety"]
        self.assertEqual(safety["network_requests_added"], 0)
        self.assertFalse(safety["pdf_requested"])
        self.assertFalse(safety["raw_html_recorded"])
        self.assertFalse(safety["screenshot_saved"])
        link = payload["browser"]["pdf_link_candidates"][0]["href"]
        query = parse_qs(urlsplit(link).query)
        self.assertEqual(query["uri"], ["OFC-2026-M2A.2"])
        self.assertEqual(query["token"], ["<redacted>"])
        self.assertTrue(
            payload["browser"]["markers"]["subscription_or_access_message"]
        )


if __name__ == "__main__":
    unittest.main()
