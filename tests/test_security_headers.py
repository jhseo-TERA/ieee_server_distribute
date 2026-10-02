import unittest

from flask import Response

from web.app import app, security_headers


class SecurityHeaderTests(unittest.TestCase):
    def test_normal_pages_cannot_be_framed(self):
        with app.test_request_context("/"):
            response = security_headers(Response())

        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

    def test_pdf_can_be_framed_by_same_origin_panel(self):
        with app.test_request_context("/pdf/example-paper"):
            response = security_headers(Response())

        self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertIn("frame-ancestors 'self'", response.headers["Content-Security-Policy"])


if __name__ == "__main__":
    unittest.main()
