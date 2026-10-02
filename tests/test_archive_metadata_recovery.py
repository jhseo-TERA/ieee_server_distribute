from scripts.recover_archive_metadata import build_manifest, clean_title, matches_ieee_document, metadata_from_item, publication_year, registered_source
import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory


def test_identity_requires_exact_ieee_host_and_document_id():
    record = lambda url: {"resource": {"primary": {"URL": url}}}
    assert matches_ieee_document(record("http://ieeexplore.ieee.org/document/12345/"), "12345")
    assert matches_ieee_document(record("https://ieeexplore.ieee.org/stamp/stamp.jsp?arnumber=12345"), "12345")
    assert not matches_ieee_document(record("https://example.org/document/12345/"), "12345")
    assert not matches_ieee_document(record("https://ieeexplore.ieee.org/document/123456/"), "12345")
    assert not matches_ieee_document({"DOI": "10.1109/ISSCC.2020.12345", "title": ["Same title"]}, "12345")


def test_unknown_or_ambiguous_venue_is_not_classified():
    sources = [{"name": "ISCAS", "system": "ieee", "type": "conference",
                "title_pattern": "International Symposium on Circuits and Systems"}]
    assert registered_source({"container-title": ["Unknown Workshop"]}, sources) is None
    assert registered_source({"container-title": ["2020 IEEE International Symposium on Circuits and Systems"]}, sources)["name"] == "ISCAS"
    assert registered_source({"container-title": ["2020 IEEE International Symposium on Circuits and Systems"]}, sources + [{**sources[0], "name": "Other"}]) is None


def test_only_reviewable_metadata_is_proposed():
    old = {"id": 8, "article_number": "12345", "title": "Recovered", "authors": None,
           "year": "2026", "source_name": "IEEE Archive", "source_type": "archive",
           "doi": None, "pdf_available": 1, "url": "keep", "pdf_local_path": "keep.pdf"}
    item = {"title": ["A &amp; B"], "author": [{"given": "Ada", "family": "Lovelace"}],
            "DOI": "10.1109/example.2020.12345", "published": {"date-parts": [[2020]]},
            "container-title": ["Unknown Symposium"]}
    new = metadata_from_item(item, old, [])
    assert new == {"title": "A & B", "authors": "Ada Lovelace", "year": "2020",
                   "source_name": "IEEE Archive", "source_type": "archive", "doi": "10.1109/example.2020.12345"}
    assert old["article_number"] == "12345" and old["pdf_local_path"] == "keep.pdf"


class ArchiveMetadataRecoveryTests(unittest.TestCase):
    test_identity_requires_exact_ieee_host_and_document_id = staticmethod(test_identity_requires_exact_ieee_host_and_document_id)
    test_unknown_or_ambiguous_venue_is_not_classified = staticmethod(test_unknown_or_ambiguous_venue_is_not_classified)
    test_only_reviewable_metadata_is_proposed = staticmethod(test_only_reviewable_metadata_is_proposed)

    def test_null_published_date_falls_back_and_legacy_issn_remains_journal(self):
        item = {"published": {"date-parts": [[None]]}, "issued": {"date-parts": [[2006]]},
                "ISSN": ["1057-7122"]}
        self.assertEqual(publication_year(item), "2006")
        self.assertIsNone(publication_year({"published": {"date-parts": [[None]]}}))
        source = registered_source(item, [{"name": "TCAS-I", "system": "ieee", "type": "journal",
                                           "issn": "1549-8328", "issn_aliases": ["1057-7122"]}])
        self.assertEqual(source["name"], "TCAS-I")
        self.assertEqual(source["type"], "journal")

    def test_title_decodes_nested_entities_without_losing_exponents(self):
        self.assertEqual(clean_title("2&lt;sup&gt;7&lt;/sup&gt;-1 PRBS in 0.13-&amp;#x03BC;m CMOS"),
                         "2^7-1 PRBS in 0.13-μm CMOS")
        self.assertEqual(clean_title(r"A $2\times 50\ Gb/s$ link in 0.18-$\mu{\hbox {m}}$ CMOS"),
                         "A 2× 50 Gb/s link in 0.18-μm CMOS")
        self.assertEqual(clean_title("3<sup>rd</sup>-Generation"), "3rd-Generation")

    def test_pdf_fallback_keeps_doi_blank_and_requires_embedded_identity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            put = lambda name, data: (root / name).write_text(json.dumps(data), encoding="utf-8")
            old = {"id": 1, "article_number": "123", "title": "Actual title", "authors": None,
                   "doi": None, "year": "2020", "source_name": "IEEE Archive", "source_type": "archive"}
            put("current_rows.json", [old])
            put("crossref_dois.json", {"supplement": {"item": {
                "type": "component", "DOI": "10.1109/example.2020.123/mm1",
                "title": ["Presentation attachment"],
                "resource": {"primary": {"URL": "https://ieeexplore.ieee.org/document/123/"}}
            }}})
            put("crossref_title_search.json", {})
            put("pdf_verified_fallbacks.json", {"123": {"authors": "Ada Lovelace", "review": "First page"}})
            put("config.json", {"sources": []})
            pdf = {"123": {"metadata": {"/Title": "Actual title", "/IEEE Article ID": "999"},
                           "text": "Actual title\nAda Lovelace"}}
            put("pdf_first_pages.json", pdf)
            (root / "123.pdf").write_bytes(b"unchanged evidence bytes")
            result = build_manifest(root, root / "config.json", root)
            self.assertEqual(result["rows"][0]["status"], "quarantine")
            pdf["123"]["metadata"]["/IEEE Article ID"] = "123"
            put("pdf_first_pages.json", pdf)
            result = build_manifest(root, root / "config.json", root)
            self.assertEqual(result["rows"][0]["status"], "ready")
            self.assertIsNone(result["rows"][0]["new"]["doi"])
            self.assertEqual(set(result["rows"][0]["changes"]), {"authors"})
            self.assertEqual((root / "123.pdf").read_bytes(), b"unchanged evidence bytes")


if __name__ == "__main__":
    unittest.main()
