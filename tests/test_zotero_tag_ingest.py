import unittest
from unittest.mock import patch

from scripts import zotero_tag_ingest


def attachment(key, mode, *, tags=None, filename=None, version=1):
    data = {
        "key": key,
        "itemType": "attachment",
        "contentType": "application/pdf",
        "linkMode": mode,
        "filename": filename or f"{key}.pdf",
        "tags": [{"tag": tag} for tag in (tags or [])],
        "dateAdded": "2026-08-10T00:00:00Z",
        "dateModified": "2026-08-10T00:00:00Z",
    }
    return {"key": key, "version": version, "data": data}


class FakeResponse:
    def __init__(self, payload, total):
        self._payload = payload
        self.headers = {"Total-Results": str(total)}

    def json(self):
        return self._payload


class ZoteroTagIngestTests(unittest.TestCase):
    def test_pdf_link_prefers_canonical_stored_attachment(self):
        children = [
            attachment("LINKED01", "linked_file", tags=["PaperServerLinkedPDF"]),
            attachment("STORED01", "imported_file"),
            attachment(
                "CANON001",
                "imported_file",
                tags=["PaperServerCanonicalPDF"],
                filename="paper.pdf",
            ),
        ]

        self.assertEqual(
            zotero_tag_ingest.pdf_link_markdown(children),
            "[paper.pdf](zotero://open-pdf/library/items/CANON001)",
        )

    def test_child_version_change_updates_fingerprint(self):
        item = {
            "key": "PARENT01",
            "version": 5,
            "data": {"key": "PARENT01", "dateModified": "2026-08-10T00:00:00Z"},
        }
        first = zotero_tag_ingest.item_fingerprint(
            item, [attachment("ATTACH01", "imported_file", version=1)]
        )
        second = zotero_tag_ingest.item_fingerprint(
            item, [attachment("ATTACH01", "imported_file", version=2)]
        )

        self.assertNotEqual(first, second)

    def test_pagination_fetches_more_than_one_hundred_items(self):
        pages = [
            FakeResponse([{"key": str(index)} for index in range(100)], 101),
            FakeResponse([{"key": "100"}], 101),
        ]
        with patch.object(zotero_tag_ingest, "zotero_get", side_effect=pages) as get:
            items = zotero_tag_ingest.zotero_get_all("/items", tag="pin")

        self.assertEqual(len(items), 101)
        self.assertEqual(get.call_args_list[0].kwargs["start"], 0)
        self.assertEqual(get.call_args_list[1].kwargs["start"], 100)


if __name__ == "__main__":
    unittest.main()
