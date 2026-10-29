import unittest
from scripts.resolve_jlt_ieee_routes import ieee_url

class JltRoutesTests(unittest.TestCase):
    def test_document_redirect(self):
        self.assertEqual(ieee_url("https://ieeexplore.ieee.org/abstract/document/12345"),
                         "https://ieeexplore.ieee.org/document/12345")
    def test_legacy_redirect(self):
        self.assertEqual(ieee_url("https://ieeexplore.ieee.org/xpl/articleDetails.jsp?arnumber=12345"),
                         "https://ieeexplore.ieee.org/document/12345")
    def test_reject_untrusted_or_non_document(self):
        for url in ["https://example.org/document/12345", "https://ieeexplore.ieee.org.evil.test/document/12345",
                    "http://ieeexplore.ieee.org/document/12345", "https://ieeexplore.ieee.org/document/abc"]:
            self.assertIsNone(ieee_url(url))
