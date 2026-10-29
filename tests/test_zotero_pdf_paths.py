import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import sync_zotero_pdf_attachments as sync


def parent(key="PARENT01", collection="TARGET", article_number="12345"):
    return {"key": key, "data": {
        "key": key, "itemType": "journalArticle", "collections": [collection],
        "extra": f"PaperServer article_number: {article_number}",
    }}


def attachment(path, *, key="LINKED01", mode="linked_file", managed=True):
    return {"key": key, "data": {
        "key": key, "version": 4, "parentItem": "PARENT01",
        "itemType": "attachment", "contentType": "application/pdf",
        "linkMode": mode, "path": str(path),
        "tags": [{"tag": sync.LINKED_TAG}] if managed else [],
        "relations": {"dc:relation": ["https://example.com/related"]},
    }}


class ZoteroPDFPathTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.pdf = Path(self.directory.name) / "paper.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 test")
        self.db = {"12345": {"absolute_pdf_path": str(self.pdf)}}

    def test_repairs_owned_path_in_place_preserving_key_version_and_metadata(self):
        linked = attachment(Path(self.directory.name) / "old" / "paper.pdf")
        original = copy.deepcopy(linked)
        updates = sync.linked_path_updates(self.db, [parent(), linked], "TARGET")
        self.assertEqual(len(updates), 1)
        expected = copy.deepcopy(linked["data"])
        expected["path"] = str(self.pdf.resolve())
        self.assertEqual(updates, [expected])
        self.assertEqual(linked, original)

    def test_leaves_correct_relative_stored_and_user_attachments_untouched(self):
        items = [parent(), attachment(self.pdf)]
        items += [attachment("attachments:paper.pdf", key="RELATIVE")]
        items += [attachment("old.pdf", key="STORED", mode="imported_file")]
        items += [attachment("old.pdf", key="USER", managed=False)]
        pending = attachment("old.pdf", key="PENDING")
        pending["data"]["tags"].append({"tag": sync.PENDING_TAG})
        deleted = attachment("old.pdf", key="DELETED")
        deleted["data"]["deleted"] = 1
        items.extend([pending, deleted])
        self.assertEqual(sync.linked_path_updates(self.db, items, "TARGET"), [])

    def test_does_not_repair_missing_files_or_ambiguous_out_of_scope_parents(self):
        linked = attachment("old.pdf")
        for parents in ([parent(collection="OTHER")], [parent(), parent("PARENT02")]):
            with self.subTest(parents=parents):
                self.assertEqual(
                    sync.linked_path_updates(self.db, parents + [linked], "TARGET"), []
                )
        self.pdf.unlink()
        self.assertEqual(
            sync.linked_path_updates(self.db, [parent(), linked], "TARGET"), []
        )

    def test_dry_run_reports_repairs_without_writing_to_zotero(self):
        client = Mock()
        client.fetch_all_items.return_value = [parent(), attachment("old.pdf")]
        client.fetch_collections.return_value = [{
            "key": "TARGET", "data": {"name": sync.DEFAULT_COLLECTION}
        }]
        with (
            patch.dict(os.environ, {"ZOTERO_USER_ID": "1", "ZOTERO_API_KEY": "test"}),
            patch.object(sync, "ZoteroClient", return_value=client),
            patch.object(sync, "fetch_db_favorites", return_value=self.db),
            patch.object(sync, "write_json") as report,
            patch("sys.argv", ["sync"]),
        ):
            self.assertEqual(sync.main(), 0)
        client.write_objects.assert_not_called()
        self.assertEqual(len(report.call_args.args[1]["linked_path_updates"]), 1)

    def test_local_setup_adds_link_alongside_stored_or_user_attachments(self):
        for existing in (
            attachment("paper.pdf", mode="imported_file"),
            attachment("old.pdf", managed=False),
            attachment("attachments:paper.pdf"),
        ):
            with self.subTest(existing=existing):
                items = [parent(), existing]
                self.assertEqual(sync.attachment_plan(self.db, items, "TARGET"), ([], []))
                plan, unsafe = sync.attachment_plan(
                    self.db, items, "TARGET", ensure_local_links=True
                )
                self.assertEqual(len(plan), 1)
                self.assertEqual(unsafe, [])
        for existing in (attachment(self.pdf), attachment("old.pdf")):
            self.assertEqual(sync.attachment_plan(
                self.db, [parent(), existing], "TARGET", ensure_local_links=True
            ), ([], []))

    def test_apply_repairs_paths_and_shares_limit_with_new_attachments(self):
        other_parent = parent("PARENT02", article_number="67890")
        before = [parent(), other_parent, attachment("old.pdf")]
        after = [parent(), other_parent, attachment(self.pdf)]
        db = {**self.db, "67890": {"absolute_pdf_path": str(self.pdf)}}
        client = Mock()
        client.fetch_all_items.side_effect = [before, after]
        client.fetch_collections.return_value = [{
            "key": "TARGET", "data": {"name": sync.DEFAULT_COLLECTION}
        }]
        client.write_objects.side_effect = lambda objects, **kwargs: len(objects)
        with (
            patch.dict(os.environ, {"ZOTERO_USER_ID": "1", "ZOTERO_API_KEY": "test"}),
            patch.object(sync, "ZoteroClient", return_value=client),
            patch.object(sync, "fetch_db_favorites", return_value=db),
            patch.object(sync, "write_json") as report,
            patch.object(sync, "create_linked_file_objects") as create,
            patch("sys.argv", ["sync", "--apply", "--limit", "1"]),
        ):
            self.assertEqual(sync.main(), 0)
        create.assert_not_called()
        self.assertEqual(client.write_objects.call_args_list[0].args[0][0]["key"], "LINKED01")
        verification = report.call_args_list[-1].args[1]
        self.assertEqual(verification["path_repairs_completed"], 1)
        self.assertEqual(verification["remaining_path_repairs"], 0)
        self.assertEqual(verification["remaining"], 1)
        self.assertEqual(verification["db_items_with_local_pdf_link"], 1)
        self.assertEqual(verification["missing_local_pdf_links"], ["67890"])


if __name__ == "__main__":
    unittest.main()
