# -*- coding: utf-8 -*-
r"""Zotero 개인 라이브러리를 SQL 즐겨찾기 전용 상태로 엄격 정리한다.

최종 상태:

* My Library 상위 항목 = DB 즐겨찾기
* 모든 DB 즐겨찾기는 지정 컬렉션 하나에만 소속
* Unfiled Items = 0
* ``--empty-trash`` 사용 시 Zotero 휴지통 = 0

기본은 dry-run이며 실제 반영은 명시적인 플래그가 필요하다::

    .venv\Scripts\python.exe scripts\reconcile_zotero_library.py
    .venv\Scripts\python.exe scripts\reconcile_zotero_library.py --apply
    .venv\Scripts\python.exe scripts\reconcile_zotero_library.py --apply --empty-trash
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests

try:
    from scripts.sync_zotero_favorites import (
        DEFAULT_COLLECTION,
        DEFAULT_TAG,
        PAPER_TYPES,
        REPORT_DIR,
        ZoteroClient,
        build_zotero_item,
        choose_canonical,
        extract_article_number,
        fetch_db_favorites,
        item_key,
        resolve_collection_key,
        updated_managed_data,
        write_json,
    )
except ModuleNotFoundError:  # ``python scripts/reconcile_zotero_library.py``
    from sync_zotero_favorites import (
        DEFAULT_COLLECTION,
        DEFAULT_TAG,
        PAPER_TYPES,
        REPORT_DIR,
        ZoteroClient,
        build_zotero_item,
        choose_canonical,
        extract_article_number,
        fetch_db_favorites,
        item_key,
        resolve_collection_key,
        updated_managed_data,
        write_json,
    )

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


@dataclass
class StrictPlan:
    creates: list[dict] = field(default_factory=list)
    updates: list[dict] = field(default_factory=list)
    trash_active_keys: list[str] = field(default_factory=list)
    keep_keys: list[str] = field(default_factory=list)
    duplicate_articles: dict[str, list[str]] = field(default_factory=dict)

    @property
    def mutation_count(self):
        return len(self.creates) + len(self.updates) + len(self.trash_active_keys)

    def summary(self, db_count, active_top_count, existing_trash_count):
        return {
            "db_favorites": db_count,
            "active_top_items": active_top_count,
            "keep_sql_items": len(self.keep_keys),
            "create_missing_sql_items": len(self.creates),
            "normalize_collection_items": len(self.updates),
            "move_non_sql_active_items_to_trash": len(self.trash_active_keys),
            "existing_trash_items": existing_trash_count,
            "duplicate_article_numbers": len(self.duplicate_articles),
            "total_active_mutations": self.mutation_count,
        }


def build_strict_plan(db_favorites, top_items, collection_key, managed_tag=DEFAULT_TAG):
    by_article = {}
    for item in top_items:
        if item.get("data", {}).get("itemType") not in PAPER_TYPES:
            continue
        article = extract_article_number(item)
        if article:
            by_article.setdefault(article, []).append(item)

    plan = StrictPlan(
        duplicate_articles={
            article: [item_key(item) for item in items]
            for article, items in by_article.items()
            if article in db_favorites and len(items) > 1
        }
    )
    keep_keys = set()

    for article, row in db_favorites.items():
        existing = by_article.get(article, [])
        if not existing:
            plan.creates.append(build_zotero_item(row, managed_tag, collection_key))
            continue
        canonical = choose_canonical(existing, managed_tag, collection_key)
        key = item_key(canonical)
        keep_keys.add(key)
        original = canonical.get("data", canonical)
        updated = updated_managed_data(
            original, managed_tag, True, collection_key
        )
        # 엄격 모드에서는 다른 컬렉션 소속도 제거한다.
        updated["collections"] = [collection_key]
        if updated != original:
            plan.updates.append(updated)

    plan.keep_keys = sorted(keep_keys)
    plan.trash_active_keys = sorted(
        item_key(item) for item in top_items if item_key(item) not in keep_keys
    )
    return plan


def prepare_soft_delete_updates(client, keys):
    current = client.fetch_items_by_keys(keys)
    by_key = {item_key(item): item for item in current}
    missing = sorted(set(keys) - set(by_key))
    if missing:
        raise RuntimeError(f"Active items disappeared before trash step: {missing[:10]}")
    updates = []
    for key in keys:
        data = copy.deepcopy(by_key[key].get("data", by_key[key]))
        data["deleted"] = 1
        updates.append(data)
    return updates


def trash_delete_order(items):
    """자식이 응답에 포함된 경우 자식부터 영구 삭제한다."""
    by_key = {item_key(item): item for item in items}

    def depth(item):
        seen = set()
        parent = item.get("data", {}).get("parentItem")
        value = 0
        while parent and parent in by_key and parent not in seen:
            seen.add(parent)
            value += 1
            parent = by_key[parent].get("data", {}).get("parentItem")
        return value

    return [item_key(item) for item in sorted(items, key=depth, reverse=True)]


def current_library_version(client):
    response = client._request(
        "GET",
        f"/users/{client.user_id}/items",
        params={"format": "versions", "limit": 1},
    )
    return response.headers.get("Last-Modified-Version")


def delete_items_permanently(client, keys, batch_size=50, retries=4):
    deleted = 0
    for offset in range(0, len(keys), batch_size):
        batch = keys[offset:offset + batch_size]
        for attempt in range(retries):
            version = current_library_version(client)
            try:
                client._request(
                    "DELETE",
                    f"/users/{client.user_id}/items",
                    params={"itemKey": ",".join(batch)},
                    headers={"If-Unmodified-Since-Version": str(version)},
                )
                deleted += len(batch)
                break
            except requests.HTTPError as exc:
                if exc.response is None or exc.response.status_code != 412 or attempt + 1 >= retries:
                    raise
                time.sleep(1 + attempt)
    return deleted


def report_payload(plan, db, top_items, trash_items, collection_name, collection_key):
    return {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "dry-run",
        "collection_name": collection_name,
        "collection_key": collection_key,
        "summary": plan.summary(len(db), len(top_items), len(trash_items)),
        "create_article_numbers": sorted(
            extract_article_number(item) for item in plan.creates
        ),
        "update_keys": sorted(item.get("key", "") for item in plan.updates),
        "trash_active_keys": plan.trash_active_keys,
        "duplicate_articles": plan.duplicate_articles,
    }


def backup_payload(db, all_active_items, trash_items, collections, plan, collection_name, collection_key):
    return {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "warning": "Metadata snapshot only; attachment binaries are backed up separately when available.",
        "collection_name": collection_name,
        "collection_key": collection_key,
        "db_favorites": list(db.values()),
        "zotero_active_items": all_active_items,
        "zotero_trash_items": trash_items,
        "zotero_collections": collections,
        "planned_active_trash_keys": plan.trash_active_keys,
    }


def active_top_items(all_items):
    return [item for item in all_items if not item.get("data", {}).get("parentItem")]


def verify_state(client, db, collection_key):
    top = client.fetch_top_items()
    trash = client._fetch_paginated(f"/users/{client.user_id}/items/trash")
    articles = [extract_article_number(item) for item in top]
    collection_items = [
        item for item in top
        if item.get("data", {}).get("collections") == [collection_key]
    ]
    unfiled = [item for item in top if not item.get("data", {}).get("collections")]
    wrong_articles = sorted(
        str(article) for article in articles if article not in db
    )
    missing_articles = sorted(set(db) - {article for article in articles if article})
    return {
        "db_favorites": len(db),
        "my_library_top_items": len(top),
        "target_collection_only_items": len(collection_items),
        "unfiled_top_items": len(unfiled),
        "trash_items": len(trash),
        "wrong_active_articles": wrong_articles,
        "missing_db_articles": missing_articles,
        "ok": (
            len(top) == len(db)
            and len(collection_items) == len(db)
            and not unfiled
            and not trash
            and not wrong_articles
            and not missing_articles
        ),
    }


def main():
    parser = argparse.ArgumentParser(description="SQL 기준 Zotero 개인 라이브러리 엄격 정리")
    parser.add_argument("--apply", action="store_true", help="실제 변경 적용(기본은 dry-run)")
    parser.add_argument(
        "--empty-trash", action="store_true",
        help="Zotero 휴지통 항목을 영구 삭제(--apply 필요)",
    )
    parser.add_argument("--tag", default=DEFAULT_TAG)
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--max-changes", type=int, default=5000)
    args = parser.parse_args()
    if args.empty_trash and not args.apply:
        raise SystemExit("[오류] --empty-trash는 --apply와 함께 사용해야 합니다.")
    if not 1 <= args.batch_size <= 50:
        raise SystemExit("[오류] --batch-size는 1~50이어야 합니다.")

    user_id = (os.getenv("ZOTERO_USER_ID") or "").strip()
    api_key = (os.getenv("ZOTERO_API_KEY") or "").strip()
    if not user_id or not api_key:
        raise SystemExit("[오류] .env에 ZOTERO_USER_ID와 ZOTERO_API_KEY가 필요합니다.")

    client = ZoteroClient(user_id, api_key, timeout=60)
    client.validate_write_access()
    collections = client.fetch_collections()
    collection_key = resolve_collection_key(collections, args.collection)
    db = fetch_db_favorites()
    if not db:
        raise SystemExit("[안전 중단] DB 즐겨찾기가 0건입니다.")

    print("[조회] 활성 항목과 휴지통 전체를 백업 범위로 불러옵니다.", flush=True)
    all_active = client.fetch_all_items()
    top_items = active_top_items(all_active)
    trash_items = client._fetch_paginated(f"/users/{client.user_id}/items/trash")
    plan = build_strict_plan(db, top_items, collection_key, args.tag)
    if plan.duplicate_articles:
        raise SystemExit(
            "[안전 중단] DB 논문 중복이 다시 생겼습니다. "
            "sync_zotero_favorites.py --apply --dedupe를 먼저 실행하세요."
        )

    summary = plan.summary(len(db), len(top_items), len(trash_items))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = REPORT_DIR / f"zotero_strict_plan_{stamp}.json"
    write_json(
        report_path,
        report_payload(
            plan, db, top_items, trash_items, args.collection, collection_key
        ),
    )
    print("=" * 64)
    print("SQL -> Zotero 엄격 라이브러리 정리")
    print("=" * 64)
    for key, value in summary.items():
        print(f"{key}: {value}")
    print(f"plan: {report_path}")
    if not args.apply:
        print("[DRY-RUN] Zotero를 변경하지 않았습니다.")
        return

    expected_changes = plan.mutation_count + (len(trash_items) if args.empty_trash else 0)
    if expected_changes > args.max_changes:
        raise SystemExit(
            f"[안전 중단] 예상 변경 {expected_changes}건이 --max-changes "
            f"{args.max_changes}를 초과합니다."
        )

    backup_path = REPORT_DIR / f"zotero_strict_backup_{stamp}.json"
    write_json(
        backup_path,
        backup_payload(
            db, all_active, trash_items, collections, plan,
            args.collection, collection_key,
        ),
    )
    print(f"backup: {backup_path}", flush=True)

    updated = client.write_objects(plan.updates, batch_size=args.batch_size)
    created = client.write_objects(plan.creates, batch_size=args.batch_size)
    print(f"[적용] 컬렉션 정규화 {updated}건 / SQL 누락 생성 {created}건", flush=True)

    soft_deleted = 0
    if plan.trash_active_keys:
        delete_updates = prepare_soft_delete_updates(client, plan.trash_active_keys)
        soft_deleted = client.write_objects(delete_updates, batch_size=args.batch_size)
    print(f"[적용] SQL 밖 활성 항목 휴지통 이동 {soft_deleted}건", flush=True)

    permanently_deleted = 0
    if args.empty_trash:
        refreshed_trash = client._fetch_paginated(f"/users/{client.user_id}/items/trash")
        purge_keys = trash_delete_order(refreshed_trash)
        permanently_deleted = delete_items_permanently(
            client, purge_keys, batch_size=args.batch_size
        )
    print(f"[적용] 휴지통 영구 삭제 {permanently_deleted}건", flush=True)

    current_db = fetch_db_favorites()
    verification = verify_state(client, current_db, collection_key)
    verify_path = REPORT_DIR / f"zotero_strict_verify_{stamp}.json"
    write_json(verify_path, {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "collection_name": args.collection,
        "collection_key": collection_key,
        **verification,
    })
    print(f"verify: {verify_path}")
    if not verification["ok"]:
        raise SystemExit("[경고] 최종 엄격 상태 검증이 일치하지 않습니다.")
    print("[완료] My Library와 목표 컬렉션이 SQL 원본과 일치하고 Unfiled/Trash가 0건입니다.")


if __name__ == "__main__":
    main()
