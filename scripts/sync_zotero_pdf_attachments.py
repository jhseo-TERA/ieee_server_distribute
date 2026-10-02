# -*- coding: utf-8 -*-
"""Attach verified Paper Server PDFs to their Zotero parent items.

The target scope is deliberately strict: DB favorite, ``pdf_available=1``,
and an existing local PDF file. Metadata parents must already exist in the
``IEEE•Optica•Nature`` collection. The default mode is a read-only audit.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

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


PENDING_TAG = "PaperServerPendingUpload"

load_dotenv(ROOT / ".env")


def has_tag(item: dict, tag_name: str) -> bool:
    return any(
        tag.get("tag") == tag_name
        for tag in item.get("data", item).get("tags", [])
    )


def pdf_children(items: list[dict]) -> dict[str, list[dict]]:
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


def top_items_by_article(items: list[dict]) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        data = item.get("data", {})
        if data.get("parentItem") or data.get("deleted"):
            continue
        if data.get("itemType") not in PAPER_TYPES:
            continue
        article_number = extract_article_number(item)
        if article_number:
            result[article_number].append(item)
    return result


def attachment_plan(
    db_items: dict[str, dict],
    all_items: list[dict],
    collection_key: str,
) -> tuple[list[dict], list[str]]:
    parents = top_items_by_article(all_items)
    children = pdf_children(all_items)
    plan = []
    unsafe = []
    for article_number, row in db_items.items():
        matches = [
            item for item in parents.get(article_number, [])
            if collection_key in item.get("data", {}).get("collections", [])
        ]
        if len(matches) != 1:
            unsafe.append(article_number)
            continue
        parent = matches[0]
        parent_key = item_key(parent)
        existing = children.get(parent_key, [])
        complete = [item for item in existing if not has_tag(item, PENDING_TAG)]
        if complete:
            continue
        pending = next((item for item in existing if has_tag(item, PENDING_TAG)), None)
        plan.append(
            {
                "article_number": article_number,
                "parent_key": parent_key,
                "pdf_path": row["absolute_pdf_path"],
                "pending_attachment_key": item_key(pending) if pending else None,
            }
        )
    return plan, unsafe


def create_pending_attachment(
    client: ZoteroClient,
    parent_key: str,
    pdf_path: Path,
) -> str:
    template = client._request(
        "GET",
        "/items/new",
        params={"itemType": "attachment", "linkMode": "imported_file"},
    ).json()
    template.update(
        {
            "parentItem": parent_key,
            "linkMode": "imported_file",
            "title": pdf_path.stem,
            "contentType": "application/pdf",
            "charset": "",
            "filename": pdf_path.name,
            "tags": [{"tag": PENDING_TAG}],
        }
    )
    response = client._request(
        "POST",
        f"/users/{client.user_id}/items",
        json=[template],
        headers={"Zotero-Write-Token": uuid.uuid4().hex},
    )
    result = response.json()
    failed = result.get("failed") or {}
    if failed:
        raise RuntimeError(f"Zotero attachment item creation failed: {failed}")
    created = (result.get("successful") or {}).get("0")
    key = item_key(created) if isinstance(created, dict) else created
    if not key:
        raise RuntimeError(f"Zotero did not return an attachment key: {result}")
    return str(key)


def normalize_upload_params(raw_params) -> dict[str, str]:
    if isinstance(raw_params, dict):
        return {str(key): str(value) for key, value in raw_params.items()}
    result = {}
    if isinstance(raw_params, list):
        for entry in raw_params:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            value = entry.get("value")
            if name is not None and value is not None:
                result[str(name)] = str(value)
    return result


def post_storage_upload(url: str, payload: dict, pdf_path: Path) -> None:
    raw_params = payload.get("params")
    if raw_params:
        form = normalize_upload_params(raw_params)
        with pdf_path.open("rb") as handle:
            response = requests.post(
                url,
                data=form,
                files={"file": (pdf_path.name, handle, "application/pdf")},
                timeout=300,
            )
    else:
        prefix = str(payload.get("prefix") or "").encode("utf-8")
        suffix = str(payload.get("suffix") or "").encode("utf-8")
        body = prefix + pdf_path.read_bytes() + suffix
        response = requests.post(
            url,
            data=body,
            headers={"Content-Type": payload["contentType"]},
            timeout=300,
        )
    if response.status_code in {429, 503}:
        time.sleep(float(response.headers.get("Retry-After", "5")))
        return post_storage_upload(url, payload, pdf_path)
    response.raise_for_status()


def upload_attachment_file(
    client: ZoteroClient,
    attachment_key: str,
    pdf_path: Path,
) -> None:
    digest = hashlib.md5(pdf_path.read_bytes()).hexdigest()
    authorization = client._request(
        "POST",
        f"/users/{client.user_id}/items/{attachment_key}/file",
        data={
            "md5": digest,
            "filename": pdf_path.name,
            "filesize": pdf_path.stat().st_size,
            "mtime": int(pdf_path.stat().st_mtime * 1000),
            "params": 1,
        },
        headers={"If-None-Match": "*"},
    ).json()
    if not authorization.get("exists"):
        post_storage_upload(authorization["url"], authorization, pdf_path)
        client._request(
            "POST",
            f"/users/{client.user_id}/items/{attachment_key}/file",
            data={"upload": authorization["uploadKey"]},
            headers={"If-None-Match": "*"},
        )


def clear_pending_tag(client: ZoteroClient, attachment_key: str) -> None:
    current = client.fetch_items_by_keys([attachment_key])
    if len(current) != 1:
        raise RuntimeError(f"Attachment disappeared after upload: {attachment_key}")
    data = copy.deepcopy(current[0].get("data", current[0]))
    data["tags"] = [
        tag for tag in data.get("tags", [])
        if tag.get("tag") != PENDING_TAG
    ]
    client.write_objects([data], batch_size=1)


def create_linked_file_objects(
    client: ZoteroClient,
    actions: list[dict],
    all_items: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Build linked-file children and trash only our own incomplete uploads."""
    template = client._request(
        "GET",
        "/items/new",
        params={"itemType": "attachment", "linkMode": "linked_file"},
    ).json()
    by_key = {item_key(item): item for item in all_items}
    cleanup = []
    linked = []
    for action in actions:
        pending_key = action.get("pending_attachment_key")
        if pending_key:
            pending = by_key.get(pending_key)
            if pending and has_tag(pending, PENDING_TAG):
                data = copy.deepcopy(pending.get("data", pending))
                data["deleted"] = 1
                cleanup.append(data)

        pdf_path = Path(action["pdf_path"]).resolve()
        attachment = copy.deepcopy(template)
        attachment.update(
            {
                "parentItem": action["parent_key"],
                "linkMode": "linked_file",
                "title": pdf_path.stem,
                "contentType": "application/pdf",
                "charset": "",
                "path": str(pdf_path),
                "tags": [{"tag": "PaperServerLinkedPDF"}],
            }
        )
        linked.append(attachment)
    return cleanup, linked


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Attach verified Paper Server PDFs to Zotero items"
    )
    parser.add_argument("--apply", action="store_true", help="perform uploads")
    parser.add_argument(
        "--mode",
        choices=("linked", "stored"),
        default="linked",
        help="linked: keep files in Paper Server; stored: upload to Zotero storage",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="maximum attachments for this invocation",
    )
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be at least 1")

    user_id = (os.getenv("ZOTERO_USER_ID") or "").strip()
    api_key = (os.getenv("ZOTERO_API_KEY") or "").strip()
    if not user_id or not api_key:
        raise SystemExit("ZOTERO_USER_ID and ZOTERO_API_KEY are required")

    client = ZoteroClient(user_id, api_key, timeout=60)
    client.validate_write_access()
    collection_key = resolve_collection_key(
        client.fetch_collections(), DEFAULT_COLLECTION
    )
    db_items = fetch_db_favorites()
    all_items = client.fetch_all_items()
    plan, unsafe = attachment_plan(db_items, all_items, collection_key)
    if unsafe:
        raise SystemExit(
            f"[safety stop] {len(unsafe)} DB items do not have exactly one "
            "parent in the target collection"
        )
    full_plan_count = len(plan)
    if args.limit is not None:
        plan = plan[:args.limit]

    total_bytes = sum(Path(item["pdf_path"]).stat().st_size for item in plan)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = REPORT_DIR / f"zotero_pdf_sync_plan_{stamp}.json"
    write_json(
        report_path,
        {
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "mode": "apply" if args.apply else "dry-run",
            "attachment_mode": args.mode,
            "collection": DEFAULT_COLLECTION,
            "collection_key": collection_key,
            "db_file_backed_items": len(db_items),
            "full_attachment_actions": full_plan_count,
            "attachment_actions": len(plan),
            "total_bytes": total_bytes,
            "items": plan,
        },
    )
    print(f"DB file-backed items: {len(db_items)}")
    print(f"PDF attachment actions: {len(plan)}")
    print(f"Upload bytes: {total_bytes:,}")
    print(f"plan: {report_path}")
    if not args.apply:
        print("[DRY-RUN] Zotero was not changed.")
        return 0
    if not plan:
        print("[OK] Every file-backed Zotero parent already has a PDF attachment.")
        return 0

    backup_path = REPORT_DIR / f"zotero_pdf_sync_backup_{stamp}.json"
    write_json(
        backup_path,
        {
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "target_items": plan,
            "zotero_items_before": all_items,
        },
    )
    print(f"backup: {backup_path}")

    completed = 0
    if args.mode == "linked":
        cleanup, linked = create_linked_file_objects(client, plan, all_items)
        if cleanup:
            removed = client.write_objects(cleanup, batch_size=50)
            print(f"[cleanup] incomplete stored attachments moved to trash: {removed}")
        created = client.write_objects(linked, batch_size=50)
        if created != len(linked):
            raise RuntimeError(
                f"Linked attachment count mismatch: {created}/{len(linked)}"
            )
        completed = created
        print(f"[linked] created {created} PDF attachments")
    else:
        for index, action in enumerate(plan, 1):
            pdf_path = Path(action["pdf_path"])
            key = action["pending_attachment_key"]
            if not key:
                key = create_pending_attachment(
                    client, action["parent_key"], pdf_path
                )
            print(
                f"[{index}/{len(plan)}] {action['article_number']} "
                f"{pdf_path.stat().st_size // 1024}KB",
                flush=True,
            )
            upload_attachment_file(client, key, pdf_path)
            clear_pending_tag(client, key)
            completed += 1

    verify_items = client.fetch_all_items()
    remaining, unsafe_after = attachment_plan(
        db_items, verify_items, collection_key
    )
    verify_path = REPORT_DIR / f"zotero_pdf_sync_verify_{stamp}.json"
    write_json(
        verify_path,
        {
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "completed": completed,
            "remaining": len(remaining),
            "expected_remaining": max(0, full_plan_count - completed),
            "unsafe": unsafe_after,
            "remaining_items": remaining,
        },
    )
    print(f"verify: {verify_path}")
    if unsafe_after or len(remaining) != max(0, full_plan_count - completed):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
