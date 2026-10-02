# -*- coding: utf-8 -*-
"""Plan and safely apply the Paper Server Zotero stored-PDF migration.

The default mode is read-only.  It identifies one byte-identical stored PDF to
keep per managed parent, exact duplicate stored attachments that can be moved
to the Zotero trash, and parents that still need a stored upload.  Cleanup is
separate from uploading so the library can be deduplicated before enabling a
larger Zotero Storage plan.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

try:
    from .download_guard import validate_pdf_file
    from .sync_zotero_favorites import (
        DEFAULT_COLLECTION,
        PAPER_TYPES,
        REPORT_DIR,
        ROOT,
        ZoteroClient,
        extract_article_number,
        fetch_db_favorites,
        item_key,
        resolve_collection_key,
        write_json,
    )
except ImportError:
    from download_guard import validate_pdf_file
    from sync_zotero_favorites import (
        DEFAULT_COLLECTION,
        PAPER_TYPES,
        REPORT_DIR,
        ROOT,
        ZoteroClient,
        extract_article_number,
        fetch_db_favorites,
        item_key,
        resolve_collection_key,
        write_json,
    )


CANONICAL_TAG = "PaperServerCanonicalPDF"
LINKED_TAG = "PaperServerLinkedPDF"
DEFAULT_STORAGE_ROOT = Path.home() / "Zotero" / "storage"

load_dotenv(ROOT / ".env")


def tag_names(item: dict) -> set[str]:
    data = item.get("data", item)
    return {
        str(tag.get("tag")) for tag in data.get("tags", []) if tag.get("tag")
    }


def active_pdf_children(items: list[dict]) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        data = item.get("data", {})
        if (
            data.get("parentItem")
            and data.get("itemType") == "attachment"
            and data.get("contentType") == "application/pdf"
            and not data.get("deleted")
        ):
            result[str(data["parentItem"])].append(item)
    return result


def managed_parents(items: list[dict], collection_key: str) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        data = item.get("data", {})
        if data.get("parentItem") or data.get("deleted"):
            continue
        if data.get("itemType") not in PAPER_TYPES:
            continue
        if collection_key not in data.get("collections", []):
            continue
        article_number = extract_article_number(item)
        if article_number:
            result[str(article_number)].append(item)
    return result


def imported_local_path(item: dict, storage_root: Path) -> Path | None:
    data = item.get("data", {})
    filename = str(data.get("filename") or "").strip()
    key = item_key(item)
    if not filename or not key:
        return None
    return storage_root / key / filename


def file_digest(path: Path, cache: dict[Path, tuple[int, str]]) -> tuple[int, str]:
    resolved = path.resolve()
    if resolved in cache:
        return cache[resolved]
    digest = hashlib.md5()
    with resolved.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    identity = (resolved.stat().st_size, digest.hexdigest())
    cache[resolved] = identity
    return identity


def is_uploaded(item: dict) -> bool:
    data = item.get("data", {})
    return bool(data.get("md5") or item.get("links", {}).get("enclosure"))


def canonical_sort_key(item: dict) -> tuple[int, int, str, str]:
    data = item.get("data", {})
    return (
        0 if CANONICAL_TAG in tag_names(item) else 1,
        0 if is_uploaded(item) else 1,
        str(data.get("dateAdded") or ""),
        item_key(item),
    )


def add_tag_update(item: dict, tag_name: str) -> dict:
    data = copy.deepcopy(item.get("data", item))
    if tag_name not in tag_names(item):
        data.setdefault("tags", []).append({"tag": tag_name})
    return data


def compact_attachment(item: dict) -> dict:
    data = item.get("data", {})
    enclosure = item.get("links", {}).get("enclosure", {})
    return {
        "key": item_key(item),
        "version": item.get("version") or data.get("version"),
        "parentItem": data.get("parentItem"),
        "linkMode": data.get("linkMode"),
        "filename": data.get("filename"),
        "path": data.get("path"),
        "dateAdded": data.get("dateAdded"),
        "uploaded": is_uploaded(item),
        "server_length": enclosure.get("length"),
        "tags": sorted(tag_names(item)),
    }


def build_storage_migration_plan(
    db_items: dict[str, dict],
    all_items: list[dict],
    collection_key: str,
    storage_root: Path,
) -> dict:
    parents = managed_parents(all_items, collection_key)
    children = active_pdf_children(all_items)
    digest_cache: dict[Path, tuple[int, str]] = {}
    keep = []
    duplicate_imported = []
    corrupt_imported = []
    create_stored = []
    linked_after_upload = []
    unsafe = []
    final_bytes = 0
    upload_bytes = 0

    for article_number, row in sorted(db_items.items()):
        matches = parents.get(article_number, [])
        if len(matches) != 1:
            unsafe.append({
                "article_number": article_number,
                "reason": "managed_parent_count",
                "count": len(matches),
            })
            continue

        parent_key = item_key(matches[0])
        source = Path(row["absolute_pdf_path"]).resolve()
        if not source.is_file():
            unsafe.append({
                "article_number": article_number,
                "parent_key": parent_key,
                "reason": "source_pdf_missing",
                "path": str(source),
            })
            continue
        source_identity = file_digest(source, digest_cache)
        final_bytes += source_identity[0]

        pdfs = children.get(parent_key, [])
        imported = [
            item for item in pdfs
            if item.get("data", {}).get("linkMode") == "imported_file"
        ]
        linked = [
            item for item in pdfs
            if item.get("data", {}).get("linkMode") == "linked_file"
            and LINKED_TAG in tag_names(item)
        ]

        matching_imported = []
        nonmatching_imported = []
        for attachment in imported:
            local_path = imported_local_path(attachment, storage_root)
            if local_path is None or not local_path.is_file():
                nonmatching_imported.append({
                    **compact_attachment(attachment),
                    "reason": "local_stored_file_missing",
                    "local_path": str(local_path) if local_path else None,
                })
                continue
            if file_digest(local_path, digest_cache) == source_identity:
                matching_imported.append(attachment)
            else:
                nonmatching_imported.append({
                    **compact_attachment(attachment),
                    "reason": "content_differs_from_paper_server",
                    "local_path": str(local_path),
                })

        if nonmatching_imported:
            corrupt = []
            unverified = []
            for entry in nonmatching_imported:
                local_path = entry.get("local_path")
                if local_path and Path(local_path).is_file():
                    valid, validation_error = validate_pdf_file(local_path)
                    if not valid:
                        corrupt.append({
                            "article_number": article_number,
                            "parent_key": parent_key,
                            "source_md5": source_identity[1],
                            "attachment": {
                                key: value for key, value in entry.items()
                                if key not in {"reason", "local_path"}
                            },
                            "local_path": local_path,
                            "validation_error": validation_error,
                        })
                        continue
                unverified.append(entry)
            if unverified:
                unsafe.append({
                    "article_number": article_number,
                    "parent_key": parent_key,
                    "reason": "unverified_imported_attachments",
                    "attachments": unverified,
                })
                continue
            corrupt_imported.extend(corrupt)

        matching_linked = []
        for attachment in linked:
            raw_path = attachment.get("data", {}).get("path")
            try:
                linked_path = Path(str(raw_path)).resolve()
            except (OSError, ValueError):
                continue
            if linked_path == source:
                matching_linked.append(attachment)

        if matching_imported:
            matching_imported.sort(key=canonical_sort_key)
            canonical = matching_imported[0]
            keep.append({
                "article_number": article_number,
                "parent_key": parent_key,
                "source_path": str(source),
                "source_bytes": source_identity[0],
                "source_md5": source_identity[1],
                "attachment": compact_attachment(canonical),
                "needs_upload": not is_uploaded(canonical),
                "needs_canonical_tag": CANONICAL_TAG not in tag_names(canonical),
            })
            if not is_uploaded(canonical):
                upload_bytes += source_identity[0]
            for duplicate in matching_imported[1:]:
                duplicate_imported.append({
                    "article_number": article_number,
                    "parent_key": parent_key,
                    "source_md5": source_identity[1],
                    "attachment": compact_attachment(duplicate),
                })
            for attachment in matching_linked:
                linked_after_upload.append({
                    "article_number": article_number,
                    "parent_key": parent_key,
                    "attachment": compact_attachment(attachment),
                })
        else:
            create_stored.append({
                "article_number": article_number,
                "parent_key": parent_key,
                "source_path": str(source),
                "source_bytes": source_identity[0],
                "source_md5": source_identity[1],
            })
            upload_bytes += source_identity[0]
            for attachment in matching_linked:
                linked_after_upload.append({
                    "article_number": article_number,
                    "parent_key": parent_key,
                    "attachment": compact_attachment(attachment),
                })

    summary = {
        "db_file_backed_items": len(db_items),
        "canonical_stored_to_keep": len(keep),
        "duplicate_imported_to_trash": len(duplicate_imported),
        "corrupt_imported_to_trash": len(corrupt_imported),
        "stored_attachments_to_create": len(create_stored),
        "linked_attachments_to_trash_after_upload": len(linked_after_upload),
        "unsafe_items": len(unsafe),
        "final_unique_bytes": final_bytes,
        "final_unique_decimal_gb": round(final_bytes / 1_000_000_000, 3),
        "estimated_upload_bytes": upload_bytes,
        "estimated_upload_decimal_gb": round(upload_bytes / 1_000_000_000, 3),
    }
    return {
        "summary": summary,
        "canonical_stored": keep,
        "duplicate_imported": duplicate_imported,
        "corrupt_imported": corrupt_imported,
        "create_stored": create_stored,
        "linked_after_upload": linked_after_upload,
        "unsafe": unsafe,
    }


def prepare_cleanup_updates(client: ZoteroClient, plan: dict) -> list[dict]:
    canonical = {
        entry["attachment"]["key"]: entry
        for entry in plan["canonical_stored"]
        if entry["needs_canonical_tag"]
    }
    duplicates = {
        entry["attachment"]["key"]: entry
        for entry in [*plan["duplicate_imported"], *plan["corrupt_imported"]]
    }
    keys = [*canonical, *duplicates]
    current = client.fetch_items_by_keys(keys)
    by_key = {item_key(item): item for item in current}
    missing = sorted(set(keys) - set(by_key))
    if missing:
        raise RuntimeError(f"Attachment items disappeared before cleanup: {missing[:10]}")

    updates = []
    for key, expected in canonical.items():
        item = by_key[key]
        data = item.get("data", {})
        if (
            data.get("parentItem") != expected["parent_key"]
            or data.get("linkMode") != "imported_file"
            or data.get("deleted")
        ):
            raise RuntimeError(f"Canonical attachment changed before cleanup: {key}")
        updates.append(add_tag_update(item, CANONICAL_TAG))

    for key, expected in duplicates.items():
        item = by_key[key]
        data = item.get("data", {})
        if (
            data.get("parentItem") != expected["parent_key"]
            or data.get("linkMode") != "imported_file"
            or data.get("deleted")
        ):
            raise RuntimeError(f"Duplicate attachment changed before cleanup: {key}")
        update = copy.deepcopy(data)
        update["deleted"] = 1
        updates.append(update)
    return updates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--storage-root",
        type=Path,
        default=DEFAULT_STORAGE_ROOT,
        help="local Zotero storage directory used to verify imported files",
    )
    parser.add_argument(
        "--input-backup",
        type=Path,
        help=(
            "use zotero_items_before from an existing full backup instead of "
            "slowly fetching the entire library"
        ),
    )
    parser.add_argument(
        "--collection-key",
        help="target collection key (required with --input-backup)",
    )
    parser.add_argument(
        "--apply-cleanup",
        action="store_true",
        help=(
            "tag canonical stored PDFs and move exact duplicate or structurally "
            "invalid imported files to trash"
        ),
    )
    parser.add_argument(
        "--confirm-cleanup-count",
        type=int,
        help="required exact duplicate count when --apply-cleanup is used",
    )
    args = parser.parse_args()

    user_id = (os.getenv("ZOTERO_USER_ID") or "").strip()
    api_key = (os.getenv("ZOTERO_API_KEY") or "").strip()
    if not user_id or not api_key:
        raise SystemExit("ZOTERO_USER_ID and ZOTERO_API_KEY are required")

    client = ZoteroClient(user_id, api_key, timeout=60)
    if args.input_backup:
        if not args.collection_key:
            raise SystemExit("--collection-key is required with --input-backup")
        try:
            backup_payload = json.loads(args.input_backup.read_text(encoding="utf-8"))
            all_items = backup_payload["zotero_items_before"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SystemExit(f"Unable to read full Zotero backup: {exc}") from exc
        collection_key = args.collection_key
    else:
        collection_key = resolve_collection_key(
            client.fetch_collections(), DEFAULT_COLLECTION
        )
        all_items = client.fetch_all_items()
    if args.apply_cleanup:
        client.validate_write_access()
    db_items = fetch_db_favorites()
    plan = build_storage_migration_plan(
        db_items, all_items, collection_key, args.storage_root
    )
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    plan_path = REPORT_DIR / f"zotero_storage_migration_plan_{stamp}.json"
    write_json(plan_path, {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "apply-cleanup" if args.apply_cleanup else "dry-run",
        "collection": DEFAULT_COLLECTION,
        "collection_key": collection_key,
        "storage_root": str(args.storage_root.resolve()),
        "input_backup": str(args.input_backup.resolve()) if args.input_backup else None,
        **plan,
    })

    print(json.dumps(plan["summary"], ensure_ascii=False, indent=2))
    print(f"plan: {plan_path}")
    if plan["unsafe"]:
        print("[SAFETY STOP] Unverified attachments are present; no changes were made.")
        return 2
    if not args.apply_cleanup:
        print("[DRY-RUN] Zotero was not changed.")
        return 0

    expected_count = (
        plan["summary"]["duplicate_imported_to_trash"]
        + plan["summary"]["corrupt_imported_to_trash"]
    )
    if args.confirm_cleanup_count != expected_count:
        print(
            "[SAFETY STOP] --confirm-cleanup-count must exactly match "
            f"{expected_count}."
        )
        return 2

    target_keys = {
        entry["attachment"]["key"]
        for entry in [*plan["duplicate_imported"], *plan["corrupt_imported"]]
    } | {
        entry["attachment"]["key"] for entry in plan["canonical_stored"]
        if entry["needs_canonical_tag"]
    }
    backup_path = REPORT_DIR / f"zotero_storage_migration_backup_{stamp}.json"
    backup_items = [
        item for item in all_items if item_key(item) in target_keys
    ]
    write_json(backup_path, {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "plan_path": str(plan_path),
        "items_before": backup_items,
    })
    print(f"backup: {backup_path}")

    updates = prepare_cleanup_updates(client, plan)
    written = client.write_objects(updates, batch_size=50)
    if written != len(updates):
        raise RuntimeError(f"Cleanup write count mismatch: {written}/{len(updates)}")
    print(
        f"[APPLIED] canonical tags and duplicate trash updates: {written}; "
        "Zotero trash was not emptied."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
