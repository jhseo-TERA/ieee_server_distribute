import unittest

from scripts.reconcile_zotero_library import build_strict_plan, trash_delete_order
from scripts.sync_zotero_favorites import DEFAULT_TAG


def paper(key, article, *, tags=None, collections=None, parent=None):
    data = {
        "key": key,
        "version": 1,
        "itemType": "journalArticle",
        "title": key,
        "url": f"https://ieeexplore.ieee.org/document/{article}/",
        "extra": "",
        "tags": [{"tag": tag} for tag in (tags or [])],
        "collections": list(collections or []),
        "relations": {},
    }
    if parent:
        data["parentItem"] = parent
    return {"key": key, "version": 1, "data": data, "meta": {"numChildren": 0}}


def row(article):
    return {
        "article_number": article,
        "title": article,
        "authors": "A",
        "year": "2026",
        "source_name": "JSSC",
        "source_type": "journal",
        "source_system": "ieee",
        "issue": "1",
    }


class StrictReconcileTests(unittest.TestCase):
    def test_keeps_only_db_items_and_one_collection(self):
        db = {"1": row("1"), "2": row("2")}
        keep = paper(
            "KEEP", "1", tags=[DEFAULT_TAG, "UserTag"],
            collections=["TARGET", "OTHER"],
        )
        extra = paper("EXTRA", "999")
        note = {
            "key": "NOTE", "version": 1, "meta": {"numChildren": 0},
            "data": {
                "key": "NOTE", "version": 1, "itemType": "note",
                "note": "top-level", "tags": [], "collections": [],
            },
        }

        plan = build_strict_plan(db, [keep, extra, note], "TARGET")

        self.assertEqual(plan.keep_keys, ["KEEP"])
        self.assertEqual(plan.trash_active_keys, ["EXTRA", "NOTE"])
        self.assertEqual(len(plan.creates), 1)
        self.assertEqual(plan.updates[0]["collections"], ["TARGET"])
        self.assertEqual(
            {tag["tag"] for tag in plan.updates[0]["tags"]},
            {DEFAULT_TAG, "UserTag"},
        )

    def test_child_is_sorted_before_parent_for_permanent_delete(self):
        parent = paper("PARENT", "1")
        child = {
            "key": "CHILD", "version": 1, "meta": {},
            "data": {
                "key": "CHILD", "version": 1, "itemType": "attachment",
                "parentItem": "PARENT", "tags": [], "collections": [],
            },
        }
        self.assertEqual(trash_delete_order([parent, child]), ["CHILD", "PARENT"])


if __name__ == "__main__":
    unittest.main()
