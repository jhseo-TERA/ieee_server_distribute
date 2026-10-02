import tempfile
import unittest
from pathlib import Path

from pypdf import PdfWriter

from scripts.migrate_zotero_storage import build_storage_migration_plan


COLLECTION = "COLLECTION1"


def parent(article_number, key):
    return {
        "key": key,
        "data": {
            "key": key,
            "itemType": "journalArticle",
            "url": f"https://ieeexplore.ieee.org/document/{article_number}/",
            "collections": [COLLECTION],
            "tags": [],
        },
    }


def imported(key, parent_key, filename, *, uploaded=False):
    item = {
        "key": key,
        "version": 1,
        "data": {
            "key": key,
            "parentItem": parent_key,
            "itemType": "attachment",
            "linkMode": "imported_file",
            "contentType": "application/pdf",
            "filename": filename,
            "tags": [],
            "dateAdded": "2026-08-10T00:00:00Z",
        },
        "links": {},
    }
    if uploaded:
        item["data"]["md5"] = "server-md5"
    return item


def linked(key, parent_key, path):
    return {
        "key": key,
        "version": 1,
        "data": {
            "key": key,
            "parentItem": parent_key,
            "itemType": "attachment",
            "linkMode": "linked_file",
            "contentType": "application/pdf",
            "path": str(path),
            "tags": [{"tag": "PaperServerLinkedPDF"}],
        },
    }


class ZoteroStorageMigrationTests(unittest.TestCase):
    @staticmethod
    def write_pdf(path, width=612, height=792):
        writer = PdfWriter()
        writer.add_blank_page(width=width, height=height)
        with path.open("wb") as stream:
            writer.write(stream)

    def test_keeps_uploaded_identical_copy_and_trashes_only_exact_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            source.write_bytes(b"%PDF identical content")
            storage = root / "storage"
            for key in ("ATTACH01", "ATTACH02"):
                attachment_dir = storage / key
                attachment_dir.mkdir(parents=True)
                (attachment_dir / "paper.pdf").write_bytes(source.read_bytes())

            db = {"12345": {"absolute_pdf_path": str(source)}}
            items = [
                parent("12345", "PARENT01"),
                imported("ATTACH01", "PARENT01", "paper.pdf"),
                imported("ATTACH02", "PARENT01", "paper.pdf", uploaded=True),
            ]

            plan = build_storage_migration_plan(db, items, COLLECTION, storage)

        self.assertEqual(plan["summary"]["unsafe_items"], 0)
        self.assertEqual(plan["summary"]["canonical_stored_to_keep"], 1)
        self.assertEqual(plan["summary"]["duplicate_imported_to_trash"], 1)
        self.assertEqual(
            plan["canonical_stored"][0]["attachment"]["key"], "ATTACH02"
        )
        self.assertEqual(
            plan["duplicate_imported"][0]["attachment"]["key"], "ATTACH01"
        )

    def test_linked_only_parent_is_planned_for_stored_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            source.write_bytes(b"%PDF linked content")
            source_size = source.stat().st_size
            db = {"67890": {"absolute_pdf_path": str(source)}}
            items = [
                parent("67890", "PARENT02"),
                linked("LINKED01", "PARENT02", source),
            ]

            plan = build_storage_migration_plan(
                db, items, COLLECTION, root / "storage"
            )

        self.assertEqual(plan["summary"]["unsafe_items"], 0)
        self.assertEqual(plan["summary"]["stored_attachments_to_create"], 1)
        self.assertEqual(
            plan["summary"]["linked_attachments_to_trash_after_upload"], 1
        )
        self.assertEqual(plan["summary"]["estimated_upload_bytes"], source_size)

    def test_corrupt_nonmatching_imported_file_is_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            self.write_pdf(source)
            storage = root / "storage"
            attachment_dir = storage / "ATTACH03"
            attachment_dir.mkdir(parents=True)
            (attachment_dir / "paper.pdf").write_bytes(b"%PDF-1.4\ntruncated")
            db = {"24680": {"absolute_pdf_path": str(source)}}
            items = [
                parent("24680", "PARENT03"),
                imported("ATTACH03", "PARENT03", "paper.pdf"),
            ]

            plan = build_storage_migration_plan(db, items, COLLECTION, storage)

        self.assertEqual(plan["summary"]["unsafe_items"], 0)
        self.assertEqual(plan["summary"]["corrupt_imported_to_trash"], 1)
        self.assertEqual(plan["summary"]["stored_attachments_to_create"], 1)
        self.assertEqual(plan["summary"]["duplicate_imported_to_trash"], 0)

    def test_valid_nonmatching_imported_file_blocks_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            self.write_pdf(source, width=612)
            storage = root / "storage"
            attachment_dir = storage / "ATTACH04"
            attachment_dir.mkdir(parents=True)
            self.write_pdf(attachment_dir / "paper.pdf", width=500)
            db = {"13579": {"absolute_pdf_path": str(source)}}
            items = [
                parent("13579", "PARENT04"),
                imported("ATTACH04", "PARENT04", "paper.pdf"),
            ]

            plan = build_storage_migration_plan(db, items, COLLECTION, storage)

        self.assertEqual(plan["summary"]["unsafe_items"], 1)
        self.assertEqual(plan["summary"]["corrupt_imported_to_trash"], 0)


if __name__ == "__main__":
    unittest.main()
