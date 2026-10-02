import unittest

from scripts.fetch_optica_crossref import extract_ieee_document_id, extract_url
from scripts.import_excel_to_db import jlt_doi_key
from scripts.download_optica_pdfs import (
    abstract_url,
    fallback_viewmedia_url,
    ieee_document_id,
    optica_uri,
    source_url,
)


class JltCrossrefTests(unittest.TestCase):
    def test_optica_doi_only_url_is_preserved_for_navigation_and_pdf_fallback(self):
        url = 'https://opg.optica.org/prj/abstract.cfm?doi=10.1364/PRJ.586160'
        self.assertEqual(source_url('optica-doi-stable-key', url), url)
        self.assertEqual(fallback_viewmedia_url('opg.optica.org', 'optica-doi-stable-key', url),
                         'https://opg.optica.org/prj/viewmedia.cfm?doi=10.1364/PRJ.586160&seq=0')
    def test_jlt_doi_has_stable_filesystem_safe_key(self):
        self.assertEqual(
            jlt_doi_key("10.1109/JLT.2026.1234567"),
            "jlt-doi-10-1109-jlt-2026-1234567",
        )
        self.assertIsNone(jlt_doi_key("10.1364/OE.123456"))

    def test_historical_jlt_doi_prefix_is_supported(self):
        self.assertEqual(
            jlt_doi_key("10.1109/50.166771"),
            "jlt-doi-10-1109-50-166771",
        )

    def test_jlt_final_record_uses_optica_canonical_url(self):
        item = {
            "DOI": "10.1109/JLT.2025.1234567",
            "volume": "43",
            "issue": "10",
            "page": "4856-4864",
            "resource": {
                "primary": {"URL": "https://ieeexplore.ieee.org/document/1234567/"}
            },
        }
        self.assertEqual(
            extract_url(item, journal_name="JLT"),
            "https://opg.optica.org/jlt/abstract.cfm?uri=jlt-43-10-4856",
        )
        self.assertEqual(extract_ieee_document_id(item), "1234567")

    def test_jlt_early_access_keeps_ieee_url_for_navigation(self):
        item = {
            "DOI": "10.1109/JLT.2026.7654321",
            "resource": {
                "primary": {"URL": "https://ieeexplore.ieee.org/document/7654321/"}
            },
        }
        self.assertEqual(
            extract_url(item, journal_name="JLT"),
            "https://ieeexplore.ieee.org/document/7654321/",
        )
        self.assertEqual(extract_ieee_document_id(item), "7654321")

    def test_optica_downloader_uses_stored_jlt_opg_uri_instead_of_doi_key(self):
        key = "jlt-doi-10-1109-jlt-2026-3655727"
        stored = "https://opg.optica.org/jlt/abstract.cfm?uri=jlt-44-8-2996"
        self.assertEqual(source_url(key, stored), stored)
        self.assertEqual(optica_uri(stored), "jlt-44-8-2996")

    def test_optica_downloader_routes_jlt_early_access_to_ieee_document(self):
        key = "jlt-doi-10-1109-jlt-2026-3704415"
        stored = "https://ieeexplore.ieee.org/document/11568487/"
        self.assertEqual(source_url(key, stored), stored)
        self.assertEqual(ieee_document_id(stored), "11568487")

    def test_optica_downloader_rejects_untrusted_stored_url(self):
        key = "oe-34-1-99"
        self.assertEqual(
            source_url(key, "https://example.invalid/redirect"),
            "https://opg.optica.org/oe/abstract.cfm?uri=oe-34-1-99",
        )

    def test_ofc_uses_root_abstract_and_viewmedia_paths(self):
        key = "OFC-2026-M2A.2"
        self.assertEqual(
            abstract_url(key),
            "https://opg.optica.org/abstract.cfm?uri=OFC-2026-M2A.2",
        )
        self.assertEqual(
            fallback_viewmedia_url("opg.optica.org", key),
            "https://opg.optica.org/viewmedia.cfm?uri=OFC-2026-M2A.2&seq=0",
        )

    def test_optica_uri_is_case_insensitive_for_ofc_urls(self):
        self.assertEqual(
            optica_uri(
                "https://opg.optica.org/abstract.cfm?URI=OFC-2026-W2A.12"
            ),
            "OFC-2026-W2A.12",
        )


if __name__ == "__main__":
    unittest.main()
