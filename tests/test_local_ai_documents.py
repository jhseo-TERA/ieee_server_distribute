import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

from local_ai_documents import DocumentError, DocumentStore, MAX_PDF_BYTES, retrieve


class LocalAiDocumentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "ieee-pdf").mkdir()
        self.path = self.root / "ieee-pdf" / "123.pdf"
        self.path.write_bytes(b"%PDF-mocked PDF contents")
        self.paper = {"id": 7, "article_number": "123", "title": "A 112 Gb/s Receiver",
                      "source_system": "ieee", "pdf_local_path": "ieee-pdf/123.pdf"}
        self.store = DocumentStore(self.root)

    @staticmethod
    def reader(texts):
        reader = MagicMock()
        reader.is_encrypted = False
        reader.pages = [MagicMock() for _ in texts]
        for page, text in zip(reader.pages, texts):
            page.extract_text.return_value = text
        return reader

    def test_path_nested_designcon_and_publisher_boundaries(self):
        nested = self.root / "DesignCon" / "2026" / "slides"
        nested.mkdir(parents=True)
        source = nested / "paper.pdf"
        source.write_bytes(b"%PDF-fixture")
        paper = {**self.paper, "source_system": "designcon", "pdf_local_path": "DesignCon\\2026\\slides\\paper.pdf"}
        self.assertEqual(self.store.resolve_path(paper), source.resolve())
        for bad in ("../private.pdf", "DesignCon/2026/slides/paper.pdf", "ieee-pdf/../private.pdf"):
            with self.subTest(path=bad), self.assertRaises(DocumentError):
                self.store.resolve_path({**self.paper, "pdf_local_path": bad})

    def test_symlink_escape_rejected(self):
        outside = self.root / "private.pdf"
        outside.write_bytes(b"%PDF-private")
        link = self.root / "ieee-pdf" / "link.pdf"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("This Windows account cannot create symlinks.")
        with self.assertRaises(DocumentError):
            self.store.resolve_path({**self.paper, "pdf_local_path": "ieee-pdf/link.pdf"})

    def test_invalid_identifier_missing_wrong_extension_and_header(self):
        for article in ("../private", "http:secret", "folder/name"):
            with self.subTest(article=article), self.assertRaises(DocumentError):
                self.store.resolve_path({**self.paper, "pdf_local_path": None, "article_number": article})
        with self.assertRaises(DocumentError):
            self.store.resolve_path({**self.paper, "source_system": "remote"})
        self.path.write_bytes(b"<html>not a PDF</html>")
        with self.assertRaises(DocumentError):
            self.store.read_pages(self.paper)

    def test_size_limit_and_cache_location_guard(self):
        with patch("local_ai_documents.MAX_PDF_BYTES", 5), self.assertRaises(DocumentError):
            self.store.read_pages(self.paper)
        with self.assertRaises(DocumentError):
            DocumentStore(self.root, self.root.parent / "other-cache")
        self.assertEqual(MAX_PDF_BYTES, 64 * 1024 * 1024)

    def test_page_evidence_cache_is_immutable_by_hash_and_source_unchanged(self):
        original = self.path.read_bytes()
        reader = self.reader(["Abstract: A receiver", "Power is 10 mW at 112 Gb/s.", "Conclusion"])
        with patch("local_ai_documents.PdfReader", return_value=reader):
            first = self.store.read_pages(self.paper, pages=[2, 1])
            second = self.store.read_pages({**self.paper, "id": 8, "title": "Updated title"}, pages=[1, 2])
        self.assertEqual([item["page"] for item in first], [1, 2])
        self.assertEqual(first[0]["paper_id"], 7)
        self.assertEqual(second[0]["paper_id"], 8)
        self.assertEqual(second[0]["title"], "Updated title")
        self.assertEqual(first[1]["source_url"], "/pdf/123#page=2")
        self.assertEqual(first[0]["pdf_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(self.path.read_bytes(), original)
        for page in reader.pages[:2]:
            page.extract_text.assert_called_once()
        old_cache = list(self.store.cache_dir.rglob("*.json"))
        self.assertEqual(len(old_cache), 2)
        old_contents = {str(path): path.read_bytes() for path in old_cache}
        self.path.write_bytes(original + b"new version")
        with patch("local_ai_documents.PdfReader", return_value=self.reader(["Updated PDF text"])):
            changed = self.store.read_pages(self.paper)
        self.assertNotEqual(first[0]["pdf_sha256"], changed[0]["pdf_sha256"])
        self.assertEqual(len(list(self.store.cache_dir.rglob("*.json"))), 3)
        self.assertTrue(all(Path(path).read_bytes() == data for path, data in old_contents.items()))

    def test_page_range_and_bounded_auto_extraction(self):
        for pages in ([], [0], [-1], [True], ["1"], list(range(1, 14))):
            with self.subTest(pages=pages), self.assertRaises(DocumentError):
                self.store.read_pages(self.paper, pages=pages)
        with patch("local_ai_documents.PdfReader", return_value=self.reader(["one", "two"])):
            self.assertEqual(len(self.store.read_pages(self.paper, max_pages=1)), 1)
            with self.assertRaises(DocumentError):
                self.store.read_pages(self.paper, pages=[3])
        for cap in (0, 41, True):
            with self.subTest(cap=cap), self.assertRaises(DocumentError):
                self.store.read_pages(self.paper, max_pages=cap)

    def test_encrypted_pdf_requires_available_password(self):
        reader = self.reader(["secret"])
        reader.is_encrypted = True
        reader.decrypt.return_value = 0
        with patch("local_ai_documents.PdfReader", return_value=reader), self.assertRaises(DocumentError):
            self.store.read_pages(self.paper)

    def test_text_clip_and_corrupt_cache_does_not_overwrite_old_evidence(self):
        with patch("local_ai_documents.PdfReader", return_value=self.reader(["x" * 33000])):
            result = self.store.read_pages(self.paper)
        self.assertEqual(len(result[0]["text"]), 32000)
        self.assertTrue(result[0]["text_truncated"])
        cache = next(self.store.cache_dir.rglob("*.json"))
        cache.write_text("bad old cache", encoding="utf-8")
        with patch("local_ai_documents.PdfReader", return_value=self.reader(["Fresh extraction"])):
            recovered = self.store.read_pages(self.paper)
        self.assertEqual(recovered[0]["text"], "Fresh extraction")
        self.assertEqual(cache.read_text(encoding="utf-8"), "bad old cache")

    def test_retrieval_lexical_sources_balanced_and_bounded(self):
        pages = [{"paper_id": 1, "article_number": "111", "page": 1, "text": "Receiver power 10 mW. " * 400,
                  "source_url": "/pdf/111#page=1", "pdf_sha256": "one"},
                 {"paper_id": 2, "article_number": "222", "page": 2, "text": "Receiver power 20 mW.",
                  "source_url": "/pdf/222#page=2", "pdf_sha256": "two"}]
        result = retrieve(pages, "Compare receiver power", limit=3, max_chars=2100)
        self.assertEqual({item["paper_id"] for item in result[:2]}, {1, 2})
        self.assertLessEqual(sum(len(item["text"]) for item in result), 2100)
        self.assertEqual([item["source_id"] for item in result], ["P1", "P2", "P3"])
        self.assertTrue(all(item["retrieval_method"] == "lexical" for item in result))
        self.assertTrue(all(item["text"] in pages[item["paper_id"] - 1]["text"] for item in result))

    def test_retrieval_favors_query_and_retains_page_links(self):
        pages = [{"paper_id": 1, "page": 1, "text": "Abstract noise description"},
                 {"paper_id": 1, "page": 2, "text": "Receiver energy power 10 mW", "source_url": "/pdf/1#page=2"}]
        result = DocumentStore.retrieve(pages, "수신 전력", limit=1)
        self.assertEqual(result[0]["page"], 2)
        self.assertEqual(result[0]["source_url"], "/pdf/1#page=2")
        self.assertEqual(result[0]["citation"], "PDF p. 2")

    def test_render_bounds_page_before_attempting_renderer(self):
        for page in (0, -1, True, 2001):
            with self.subTest(page=page), self.assertRaises(DocumentError):
                self.store.render_page(self.paper, page)

    def test_render_closes_pdfium_objects_without_assuming_page_context_manager(self):
        from PIL import Image
        document = MagicMock()
        document.__enter__.return_value = document
        document.__len__.return_value = 2
        closed = []
        bitmap = types.SimpleNamespace(to_pil=lambda: Image.new("RGB", (40, 60), "white"),
                                       close=lambda: closed.append("bitmap"))
        selected = types.SimpleNamespace(get_size=lambda: (400, 600), render=lambda **kwargs: bitmap,
                                         close=lambda: closed.append("page"))
        document.__getitem__.return_value = selected
        module = types.SimpleNamespace(PdfDocument=lambda *args, **kwargs: document)
        with patch.dict("sys.modules", {"pypdfium2": module}):
            picture = self.store.render_page(self.paper, 1)
        self.assertTrue(picture.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertLess(len(picture), 1_800_000)
        self.assertEqual(closed, ["bitmap", "page"])


if __name__ == "__main__":
    unittest.main()
