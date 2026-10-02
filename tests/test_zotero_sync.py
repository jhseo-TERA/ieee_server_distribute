import unittest
from datetime import datetime

from scripts.sync_zotero_favorites import (
    DEFAULT_TAG,
    build_sync_plan,
    build_zotero_item,
    choose_canonical,
    extract_article_number,
    merge_paperserver_citation_extra,
    managed_citation_snapshot,
    tag_names,
)


def item(
    key, url, *, tags=None, children=0, version=1, extra="", collections=None,
):
    return {
        "key": key,
        "version": version,
        "meta": {"numChildren": children},
        "data": {
            "key": key,
            "version": version,
            "itemType": "journalArticle",
            "title": key,
            "url": url,
            "extra": extra,
            "tags": [{"tag": tag} for tag in (tags or [])],
            "collections": ["COLL"] if collections is None else collections,
        },
    }


def child(key, parent, *, item_type="note"):
    data = {
        "key": key,
        "version": 1,
        "itemType": item_type,
        "parentItem": parent,
        "tags": [],
        "collections": [],
        "relations": {},
    }
    if item_type == "note":
        data["note"] = f"note-{key}"
    else:
        data.update({
            "title": key,
            "linkMode": "imported_file",
            "contentType": "application/pdf",
            "filename": f"{key}.pdf",
        })
    return {"key": key, "version": 1, "data": data, "meta": {}}


class ZoteroSyncTests(unittest.TestCase):
    def test_extract_article_number_from_supported_urls_and_extra(self):
        self.assertEqual(
            extract_article_number(item("A", "https://ieeexplore.ieee.org/document/12345/")),
            "12345",
        )
        self.assertEqual(
            extract_article_number(item("B", "https://www.nature.com/articles/s41566-024-x")),
            "s41566-024-x",
        )
        self.assertEqual(
            extract_article_number(item("C", "https://opg.optica.org/oe/abstract.cfm?uri=oe-1-2-3")),
            "oe-1-2-3",
        )
        self.assertEqual(
            extract_article_number(item("D", "", extra="PaperServer article_number: custom-key")),
            "custom-key",
        )

    def test_canonical_prefers_children_without_deleting_duplicates(self):
        plain = item("PLAIN", "https://ieeexplore.ieee.org/document/123/", children=0)
        rich = item("RICH", "https://ieeexplore.ieee.org/document/123/", children=2)
        self.assertEqual(choose_canonical([plain, rich])["key"], "RICH")

    def test_plan_creates_missing_and_tags_only_one_duplicate(self):
        db = {
            "123": {
                "article_number": "123", "title": "Existing", "authors": "A; B",
                "year": "2025", "source_name": "JSSC", "source_type": "journal",
                "source_system": "ieee", "issue": "1",
            },
            "456": {
                "article_number": "456", "title": "Missing", "authors": "C",
                "year": "2026", "source_name": "ISSCC", "source_type": "conference",
                "source_system": "ieee", "issue": "p.1",
            },
        }
        zotero = [
            item("DUP1", "https://ieeexplore.ieee.org/document/123/", children=0),
            item("DUP2", "https://ieeexplore.ieee.org/document/123/", children=3),
            item("OLD", "https://ieeexplore.ieee.org/document/999/", tags=[DEFAULT_TAG]),
        ]

        plan = build_sync_plan(db, zotero)

        self.assertEqual(len(plan.creates), 1)
        self.assertEqual(len(plan.tag_additions), 1)
        self.assertEqual(plan.tag_additions[0]["key"], "DUP2")
        self.assertIn(DEFAULT_TAG, tag_names(plan.tag_additions[0]))
        self.assertEqual(len(plan.tag_removals), 1)
        self.assertEqual(plan.tag_removals[0]["key"], "OLD")
        self.assertEqual(plan.duplicates["123"], ["DUP1", "DUP2"])

    def test_new_item_has_managed_tag_and_preserves_source_identity(self):
        row = {
            "article_number": "oe-34-1-99", "title": "Optical link", "authors": "A and B",
            "year": "2026", "source_name": "OE", "source_type": "journal",
            "source_system": "optica", "issue": "4",
        }
        built = build_zotero_item(row)
        self.assertEqual(built["itemType"], "journalArticle")
        self.assertEqual(built["url"], "https://opg.optica.org/oe/abstract.cfm?uri=oe-34-1-99")
        self.assertIn("PaperServer article_number: oe-34-1-99", built["extra"])
        self.assertIn(DEFAULT_TAG, tag_names(built))
        self.assertEqual([creator["name"] for creator in built["creators"]], ["A", "B"])

    def test_jlt_item_uses_stored_opg_url_instead_of_doi_key_url(self):
        row = {
            "article_number": "jlt-doi-10-1109-jlt-2026-3655727",
            "title": "JLT paper",
            "authors": "A and B",
            "year": "2026",
            "source_name": "JLT",
            "source_type": "journal",
            "source_system": "optica",
            "issue": "Vol. 44, Issue 8",
            "url": "https://opg.optica.org/jlt/abstract.cfm?uri=jlt-44-8-2996",
        }
        built = build_zotero_item(row)
        self.assertEqual(built["url"], row["url"])
        self.assertIn(
            "PaperServer article_number: jlt-doi-10-1109-jlt-2026-3655727",
            built["extra"],
        )

    def test_existing_jlt_item_gets_stored_publisher_url(self):
        article_number = "jlt-doi-10-1109-jlt-2026-3704415"
        desired_url = "https://ieeexplore.ieee.org/document/11568487/"
        db = {
            article_number: {
                "article_number": article_number,
                "title": "JLT early access",
                "authors": "A and B",
                "year": "2026",
                "source_name": "JLT",
                "source_type": "journal",
                "source_system": "optica",
                "issue": "",
                "url": desired_url,
            }
        }
        zotero = [
            item(
                "JLTITEM",
                f"https://opg.optica.org/jlt/abstract.cfm?uri={article_number}",
                tags=[DEFAULT_TAG],
                collections=["TARGET"],
                extra=f"PaperServer article_number: {article_number}",
            )
        ]
        plan = build_sync_plan(db, zotero, collection_key="TARGET")
        self.assertEqual(len(plan.item_updates), 1)
        self.assertEqual(len(plan.url_updates), 1)
        self.assertEqual(plan.item_updates[0]["url"], desired_url)

    def test_new_item_includes_sql_citation_metadata(self):
        row = {
            "article_number": "123",
            "title": "Paper",
            "authors": "A",
            "year": "2026",
            "source_name": "JSSC",
            "source_type": "journal",
            "source_system": "ieee",
            "issue": "1",
            "citation_count": 27,
            "citation_source": "IEEE",
            "citation_updated_at": datetime(2026, 8, 5, 1, 2, 3),
        }
        built = build_zotero_item(row)
        self.assertIn("PaperServer citation_count: 27", built["extra"])
        self.assertIn("PaperServer citation_source: ieee", built["extra"])
        self.assertIn(
            "PaperServer citation_updated_at: 2026-08-05T01:02:03Z",
            built["extra"],
        )

    def test_citation_merge_preserves_user_extra_and_replaces_only_managed_lines(self):
        original = (
            "DOI: 10.1000/example\n"
            "User note: keep me\n"
            "PaperServer citation_count: 3\n"
            "PaperServer citation_source: old"
        )
        merged = merge_paperserver_citation_extra(
            original,
            {
                "citation_count": 9,
                "citation_source": "crossref",
                "citation_updated_at": "2026-08-05 12:00:00",
            },
        )
        self.assertIn("DOI: 10.1000/example", merged)
        self.assertIn("User note: keep me", merged)
        self.assertNotIn("citation_count: 3", merged)
        self.assertEqual(
            managed_citation_snapshot(merged),
            (
                "PaperServer citation_count: 9",
                "PaperServer citation_source: crossref",
                "PaperServer citation_updated_at: 2026-08-05 12:00:00",
            ),
        )

    def test_existing_item_gets_citation_update_without_losing_user_extra(self):
        db = {
            "123": {
                "article_number": "123",
                "title": "Paper",
                "authors": "A",
                "year": "2026",
                "source_name": "JSSC",
                "source_type": "journal",
                "source_system": "ieee",
                "issue": "1",
                "citation_count": 42,
                "citation_source": "ieee",
                "citation_updated_at": datetime(2026, 8, 5, 0, 0, 0),
            }
        }
        zotero = [
            item(
                "PAPER",
                "https://ieeexplore.ieee.org/document/123/",
                tags=[DEFAULT_TAG],
                collections=["TARGET"],
                extra="User note: preserved",
            )
        ]
        plan = build_sync_plan(db, zotero, collection_key="TARGET")
        self.assertEqual(len(plan.item_updates), 1)
        self.assertEqual(len(plan.citation_updates), 1)
        self.assertIn("User note: preserved", plan.item_updates[0]["extra"])
        self.assertIn("PaperServer citation_count: 42", plan.item_updates[0]["extra"])

    def test_null_sql_citation_removes_stale_managed_lines(self):
        db = {
            "123": {
                "article_number": "123",
                "title": "Paper",
                "authors": "A",
                "year": "2026",
                "source_name": "JSSC",
                "source_type": "journal",
                "source_system": "ieee",
                "issue": "1",
                "citation_count": None,
                "citation_source": None,
                "citation_updated_at": None,
            }
        }
        zotero = [
            item(
                "PAPER",
                "https://ieeexplore.ieee.org/document/123/",
                tags=[DEFAULT_TAG],
                collections=["TARGET"],
                extra="User note\nPaperServer citation_count: 99",
            )
        ]
        plan = build_sync_plan(db, zotero, collection_key="TARGET")
        self.assertEqual(plan.item_updates[0]["extra"], "User note")

    def test_new_item_can_be_created_in_target_collection(self):
        row = {
            "article_number": "123", "title": "Paper", "authors": "A",
            "year": "2026", "source_name": "JSSC", "source_type": "journal",
            "source_system": "ieee", "issue": "1",
        }
        built = build_zotero_item(row, collection_key="TARGET")
        self.assertEqual(built["collections"], ["TARGET"])

    def test_crossref_ieee_comma_authors_are_split(self):
        row = {
            "article_number": "10983780",
            "title": "Example",
            "authors": "Chang Liu, Burak Catli, and Jun Cao",
            "year": "2025",
            "source_name": "CICC",
            "source_type": "conference",
            "source_system": "ieee",
            "issue": "",
        }
        built = build_zotero_item(row)
        self.assertEqual(
            [creator["name"] for creator in built["creators"]],
            ["Chang Liu", "Burak Catli", "Jun Cao"],
        )

    def test_plan_adds_existing_favorite_to_target_collection(self):
        db = {
            "123": {
                "article_number": "123", "title": "Paper", "authors": "A",
                "year": "2026", "source_name": "JSSC", "source_type": "journal",
                "source_system": "ieee", "issue": "1",
            }
        }
        zotero = [
            item(
                "PAPER", "https://ieeexplore.ieee.org/document/123/",
                tags=[DEFAULT_TAG], collections=[],
            )
        ]
        plan = build_sync_plan(db, zotero, collection_key="TARGET")
        self.assertEqual(len(plan.item_updates), 1)
        self.assertEqual(plan.item_updates[0]["collections"], ["TARGET"])
        self.assertEqual(len(plan.collection_additions), 1)

    def test_existing_managed_tag_order_is_idempotent(self):
        db = {
            "123": {
                "article_number": "123", "title": "Paper", "authors": "A",
                "year": "2026", "source_name": "JSSC", "source_type": "journal",
                "source_system": "ieee", "issue": "1",
            }
        }
        zotero = [
            item(
                "PAPER", "https://ieeexplore.ieee.org/document/123/",
                tags=[DEFAULT_TAG, "📌"], collections=["TARGET"],
            )
        ]
        plan = build_sync_plan(db, zotero, collection_key="TARGET")
        self.assertEqual(plan.item_updates, [])

    def test_dedupe_reparents_children_and_preserves_tags_and_collections(self):
        db = {
            "123": {
                "article_number": "123", "title": "Paper", "authors": "A",
                "year": "2026", "source_name": "JSSC", "source_type": "journal",
                "source_system": "ieee", "issue": "1",
            }
        }
        canonical = item(
            "KEEP", "https://ieeexplore.ieee.org/document/123/",
            tags=[DEFAULT_TAG], children=1, collections=["TARGET"],
        )
        duplicate = item(
            "DROP", "https://ieeexplore.ieee.org/document/123/",
            tags=["UserTag"], children=2, collections=["OTHER"],
        )
        children = {
            "KEEP": [child("KEEP_NOTE", "KEEP")],
            "DROP": [
                child("DROP_NOTE", "DROP"),
                child("DROP_PDF", "DROP", item_type="attachment"),
            ],
        }

        plan = build_sync_plan(
            db, [canonical, duplicate], collection_key="TARGET",
            dedupe=True, children_by_parent=children,
        )

        self.assertEqual(plan.trash_keys, ["DROP"])
        self.assertEqual(
            {entry["key"] for entry in plan.child_reparentings},
            {"DROP_NOTE", "DROP_PDF"},
        )
        self.assertTrue(all(
            entry["parentItem"] == "KEEP" for entry in plan.child_reparentings
        ))
        keep_update = next(entry for entry in plan.item_updates if entry["key"] == "KEEP")
        self.assertEqual(tag_names(keep_update), {DEFAULT_TAG, "UserTag"})
        self.assertEqual(set(keep_update["collections"]), {"TARGET", "OTHER"})

    def test_dedupe_stops_when_child_inventory_is_incomplete(self):
        db = {
            "123": {
                "article_number": "123", "title": "Paper", "authors": "A",
                "year": "2026", "source_name": "JSSC", "source_type": "journal",
                "source_system": "ieee", "issue": "1",
            }
        }
        canonical = item(
            "KEEP", "https://ieeexplore.ieee.org/document/123/",
            tags=[DEFAULT_TAG], children=1, collections=["TARGET"],
        )
        duplicate = item(
            "DROP", "https://ieeexplore.ieee.org/document/123/", children=2,
        )
        plan = build_sync_plan(
            db, [canonical, duplicate], collection_key="TARGET",
            dedupe=True, children_by_parent={"DROP": [child("ONLY_ONE", "DROP")]},
        )
        self.assertIn("123", plan.unsafe_duplicates)
        self.assertEqual(plan.trash_keys, [])
        self.assertEqual(plan.child_reparentings, [])


if __name__ == "__main__":
    unittest.main()
