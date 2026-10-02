# -*- coding: utf-8 -*-
r"""MySQL 즐겨찾기를 Zotero 개인 라이브러리에 단방향으로 미러링한다.

MySQL ``papers.is_favorite``가 유일한 원본이다. 관리 태그와 전용 컬렉션만
동기화하며, 기본 실행은 dry-run이다::

    .venv\Scripts\python.exe scripts\sync_zotero_favorites.py
    .venv\Scripts\python.exe scripts\sync_zotero_favorites.py --apply

``--dedupe``를 함께 사용하면 같은 Paper Server article_number를 가진 중복 논문을
정리한다. 중복 부모의 첨부·노트는 대표 논문으로 먼저 옮기고, 사용자 태그와 다른
컬렉션 소속은 대표 논문에 합친 뒤 중복 부모만 Zotero 휴지통으로 이동한다.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pymysql
import requests
from dotenv import load_dotenv

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "py_02_reports"
load_dotenv(ROOT / ".env")

API_BASE = "https://api.zotero.org"
API_VERSION = "3"
DEFAULT_TAG = "PaperServerFavorite"
DEFAULT_COLLECTION = "IEEE•Optica•Nature"
PAPER_TYPES = {"journalArticle", "conferencePaper"}
ARTICLE_EXTRA_RE = re.compile(
    r"^\s*(?:PaperServer\s+article_number|Article\s+Number)\s*:\s*(\S+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
MANAGED_CITATION_EXTRA_RE = re.compile(
    r"^\s*PaperServer\s+citation_(?:count|source|updated_at)\s*:.*$",
    re.IGNORECASE,
)
IEEE_URL_RE = re.compile(r"ieeexplore\.ieee\.org/document/([^/?#]+)", re.IGNORECASE)
NATURE_URL_RE = re.compile(r"nature\.com/articles/([^/?#]+)", re.IGNORECASE)

DB_CONFIG = dict(
    host=os.getenv("DB_HOST", "127.0.0.1"),
    port=int(os.getenv("DB_PORT", "3306")),
    user=os.getenv("DB_USER", "root"),
    password=os.getenv("DB_PASSWORD", ""),
    database=os.getenv("DB_NAME", "ieee_repo"),
    charset="utf8mb4",
    cursorclass=pymysql.cursors.DictCursor,
)


@dataclass
class SyncPlan:
    creates: list[dict] = field(default_factory=list)
    item_updates: list[dict] = field(default_factory=list)
    child_reparentings: list[dict] = field(default_factory=list)
    trash_keys: list[str] = field(default_factory=list)
    trash_articles: dict[str, str] = field(default_factory=dict)
    duplicates: dict[str, list[str]] = field(default_factory=dict)
    unsafe_duplicates: dict[str, dict] = field(default_factory=dict)
    metadata_conflicts: dict[str, list[str]] = field(default_factory=dict)
    unidentified_tagged_keys: list[str] = field(default_factory=list)
    tag_additions: list[dict] = field(default_factory=list)
    tag_removals: list[dict] = field(default_factory=list)
    collection_additions: list[dict] = field(default_factory=list)
    collection_removals: list[dict] = field(default_factory=list)
    citation_updates: list[dict] = field(default_factory=list)
    url_updates: list[dict] = field(default_factory=list)
    identifiable_unique: int = 0
    ignored_zotero_only: int = 0

    @property
    def mutation_count(self):
        return (
            len(self.creates)
            + len(self.item_updates)
            + len(self.child_reparentings)
            + len(self.trash_keys)
        )

    def summary(self, db_count, zotero_top_count):
        return {
            "db_favorites": db_count,
            "zotero_top_items": zotero_top_count,
            "zotero_identifiable_unique": self.identifiable_unique,
            "create_items": len(self.creates),
            "update_items": len(self.item_updates),
            "add_managed_tag": len(self.tag_additions),
            "remove_managed_tag": len(self.tag_removals),
            "add_target_collection": len(self.collection_additions),
            "remove_target_collection": len(self.collection_removals),
            "update_citation_metadata": len(self.citation_updates),
            "update_urls": len(self.url_updates),
            "reparent_child_items": len(self.child_reparentings),
            "move_duplicate_parents_to_trash": len(self.trash_keys),
            "total_mutations": self.mutation_count,
            "duplicate_article_numbers": len(self.duplicates),
            "duplicate_extra_items": sum(
                max(0, len(keys) - 1) for keys in self.duplicates.values()
            ),
            "unsafe_duplicate_groups": len(self.unsafe_duplicates),
            "metadata_conflict_groups": len(self.metadata_conflicts),
            "unidentified_tagged_items": len(self.unidentified_tagged_keys),
            "ignored_zotero_only_items": self.ignored_zotero_only,
        }


def fetch_db_favorites():
    sql = (
        "SELECT id, article_number, title, authors, year, source_name, source_type, "
        "source_system, issue, url, pdf_available, pdf_local_path, "
        "citation_count, citation_source, citation_updated_at "
        "FROM papers WHERE is_favorite=1 AND pdf_available=1 "
        "ORDER BY (year+0) DESC, id DESC"
    )
    connection = pymysql.connect(**DB_CONFIG)
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
    finally:
        connection.close()
    result = {}
    missing_files = []
    for row in rows:
        raw_path = str(row.get("pdf_local_path") or "").strip()
        candidate = (ROOT / raw_path).resolve() if raw_path else None
        try:
            if candidate is None:
                raise ValueError
            candidate.relative_to(ROOT.resolve())
        except ValueError:
            missing_files.append(str(row["article_number"]))
            continue
        if not candidate.is_file():
            missing_files.append(str(row["article_number"]))
            continue
        row["absolute_pdf_path"] = str(candidate)
        result[str(row["article_number"])] = row
    if missing_files:
        print(
            f"[경고] pdf_available=1이지만 실파일이 없는 항목 "
            f"{len(missing_files)}건을 Zotero 동기화에서 제외합니다."
        )
    return result


def split_authors(authors, source_system):
    authors = (authors or "").strip()
    if not authors:
        return []
    if source_system == "ieee":
        if ";" in authors:
            parts = authors.split(";")
        else:
            normalized = re.sub(r",?\s+and\s+", ", ", authors)
            parts = normalized.split(",")
        return [part.strip()[:255] for part in parts if part.strip()]
    normalized = re.sub(r"\s+and\s+", ", ", authors)
    return [part.strip()[:255] for part in normalized.split(",") if part.strip()]


def canonical_url(article_number, source_system, stored_url=None, source_name=None):
    if source_name == "JLT":
        candidate = str(stored_url or "").strip()
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            parsed = None
        if parsed and parsed.scheme == "https" and parsed.netloc:
            return candidate
    if source_system == "ieee":
        return f"https://ieeexplore.ieee.org/document/{article_number}/"
    if source_system == "nature":
        return f"https://www.nature.com/articles/{article_number}"
    if source_system == "designcon":
        return ""
    segment = article_number.split("-", 1)[0]
    if segment == "optica":
        return f"https://opg.optica.org/abstract.cfm?uri={article_number}"
    return f"https://opg.optica.org/{segment}/abstract.cfm?uri={article_number}"


def build_zotero_item(row, managed_tag=DEFAULT_TAG, collection_key=None):
    article_number = str(row["article_number"])
    source_system = row.get("source_system") or ""
    is_conference = row.get("source_type") == "conference"
    item = {
        "itemType": "conferencePaper" if is_conference else "journalArticle",
        "title": (row.get("title") or "").strip(),
        "creators": [
            {"creatorType": "author", "name": name}
            for name in split_authors(row.get("authors"), source_system)
        ],
        "date": str(row.get("year") or ""),
        "url": canonical_url(
            article_number,
            source_system,
            row.get("url"),
            row.get("source_name"),
        ),
        "extra": merge_paperserver_citation_extra(
            (
                f"PaperServer article_number: {article_number}\n"
                f"PaperServer source_system: {source_system}"
            ),
            row,
        ),
        "libraryCatalog": "IEEE Paper Server",
        "tags": [{"tag": managed_tag}],
        "collections": [collection_key] if collection_key else [],
    }
    source_name = row.get("source_name") or ""
    issue = row.get("issue") or ""
    if is_conference:
        item["conferenceName"] = source_name
        item["proceedingsTitle"] = source_name
        item["publisher"] = source_name
        if issue:
            item["pages"] = str(issue).removeprefix("p.")
    else:
        item["publicationTitle"] = source_name
        if issue:
            item["issue"] = str(issue).removeprefix("Issue ")
    return item


def extract_article_number(item):
    data = item.get("data", item)
    extra_match = ARTICLE_EXTRA_RE.search(data.get("extra") or "")
    if extra_match:
        return extra_match.group(1).strip()

    url = data.get("url") or ""
    match = IEEE_URL_RE.search(url)
    if match:
        return match.group(1)
    match = NATURE_URL_RE.search(url)
    if match:
        return match.group(1)
    try:
        uri_values = parse_qs(urlsplit(url).query).get("uri") or []
    except ValueError:
        uri_values = []
    return uri_values[0].strip() if uri_values else None


def item_key(item):
    return item.get("data", {}).get("key") or item.get("key") or ""


def tag_names(item):
    data = item.get("data", item)
    return {tag.get("tag") for tag in data.get("tags", []) if tag.get("tag")}


def _citation_timestamp(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value).strip()


def managed_citation_lines(row):
    """Build the PaperServer-owned citation lines for Zotero Extra."""
    if not row or row.get("citation_count") is None:
        return []
    lines = [f"PaperServer citation_count: {max(0, int(row['citation_count']))}"]
    source = str(row.get("citation_source") or "").strip().lower()
    updated_at = _citation_timestamp(row.get("citation_updated_at"))
    if source:
        lines.append(f"PaperServer citation_source: {source}")
    if updated_at:
        lines.append(f"PaperServer citation_updated_at: {updated_at}")
    return lines


def merge_paperserver_citation_extra(extra, row):
    """Replace only PaperServer citation lines and preserve all user Extra text."""
    kept = [
        line for line in str(extra or "").splitlines()
        if not MANAGED_CITATION_EXTRA_RE.match(line)
    ]
    while kept and not kept[-1].strip():
        kept.pop()
    desired = managed_citation_lines(row)
    if desired and kept:
        kept.append("")
    kept.extend(desired)
    return "\n".join(kept)


def managed_citation_snapshot(extra):
    return tuple(
        line.strip() for line in str(extra or "").splitlines()
        if MANAGED_CITATION_EXTRA_RE.match(line)
    )


def choose_canonical(items, managed_tag=DEFAULT_TAG, collection_key=None):
    """변경량을 줄이기 위해 현재 관리 항목을 우선 대표로 선택한다."""

    def quality(item):
        data = item.get("data", {})
        meta = item.get("meta", {}) or {}
        filled = sum(
            value not in (None, "", [], {})
            for key, value in data.items()
            if key not in {"key", "version", "tags", "collections", "relations"}
        )
        return (
            managed_tag in tag_names(item),
            bool(collection_key and collection_key in data.get("collections", [])),
            int(meta.get("numChildren") or 0),
            filled,
            len(data.get("tags", [])),
            int(data.get("version") or item.get("version") or 0),
            item_key(item),
        )

    return max(items, key=quality)


def _is_empty(value):
    return value in (None, "", [], {})


def _merge_relations(target, source):
    merged = copy.deepcopy(target or {})
    for predicate, source_values in (source or {}).items():
        if predicate not in merged:
            merged[predicate] = copy.deepcopy(source_values)
            continue
        target_values = merged[predicate]
        target_list = target_values if isinstance(target_values, list) else [target_values]
        source_list = source_values if isinstance(source_values, list) else [source_values]
        combined = target_list + [value for value in source_list if value not in target_list]
        merged[predicate] = combined if len(combined) > 1 else combined[0]
    return merged


def merge_duplicate_data(canonical, extras):
    """대표 항목에 중복 항목의 사용자 태그·컬렉션·비어 있던 필드를 보존한다."""
    merged = copy.deepcopy(canonical.get("data", canonical))
    conflicts = set()
    ignored = {
        "key", "version", "itemType", "parentItem", "tags", "collections",
        "relations", "dateAdded", "dateModified", "deleted",
    }
    for extra in extras:
        data = extra.get("data", extra)

        known_tags = {tag.get("tag") for tag in merged.get("tags", [])}
        for tag in data.get("tags", []):
            if tag.get("tag") and tag.get("tag") not in known_tags:
                merged.setdefault("tags", []).append(copy.deepcopy(tag))
                known_tags.add(tag.get("tag"))

        known_collections = set(merged.get("collections", []))
        for collection in data.get("collections", []):
            if collection not in known_collections:
                merged.setdefault("collections", []).append(collection)
                known_collections.add(collection)

        merged["relations"] = _merge_relations(
            merged.get("relations"), data.get("relations")
        )
        for key, value in data.items():
            if key in ignored or _is_empty(value):
                continue
            if _is_empty(merged.get(key)):
                merged[key] = copy.deepcopy(value)
            elif merged.get(key) != value:
                conflicts.add(key)
    return merged, sorted(conflicts)


def updated_managed_data(
    item_or_data,
    managed_tag,
    should_manage,
    collection_key=None,
    citation_row=None,
):
    data = copy.deepcopy(item_or_data.get("data", item_or_data))
    tags = list(data.get("tags", []))
    has_managed_tag = any(tag.get("tag") == managed_tag for tag in tags)
    if should_manage and not has_managed_tag:
        tags.append({"tag": managed_tag})
    elif not should_manage and has_managed_tag:
        tags = [tag for tag in tags if tag.get("tag") != managed_tag]
    data["tags"] = tags

    if collection_key:
        collections = list(data.get("collections", []))
        has_collection = collection_key in collections
        if should_manage and not has_collection:
            collections.append(collection_key)
        elif not should_manage and has_collection:
            collections = [key for key in collections if key != collection_key]
        data["collections"] = collections
    data["extra"] = merge_paperserver_citation_extra(
        data.get("extra"), citation_row if should_manage else None
    )
    if should_manage and citation_row and citation_row.get("source_name") == "JLT":
        desired_url = canonical_url(
            str(citation_row["article_number"]),
            citation_row.get("source_system") or "",
            citation_row.get("url"),
            citation_row.get("source_name"),
        )
        if desired_url:
            data["url"] = desired_url
    return data


def updated_tag_data(item, managed_tag, should_have_tag):
    """기존 호출부와 테스트를 위한 태그 전용 호환 함수."""
    return updated_managed_data(item, managed_tag, should_have_tag)


def build_sync_plan(
    db_favorites,
    zotero_items,
    managed_tag=DEFAULT_TAG,
    collection_key=None,
    *,
    dedupe=False,
    children_by_parent=None,
):
    children_by_parent = children_by_parent or {}
    by_article = defaultdict(list)
    unidentified_tagged = []
    for item in zotero_items:
        data = item.get("data", {})
        if data.get("itemType") not in PAPER_TYPES:
            continue
        article_number = extract_article_number(item)
        if article_number:
            by_article[article_number].append(item)
        elif managed_tag in tag_names(item):
            unidentified_tagged.append(item_key(item) or "?")

    plan = SyncPlan(
        duplicates={
            article: [item_key(item) or "?" for item in items]
            for article, items in by_article.items() if len(items) > 1
        },
        unidentified_tagged_keys=unidentified_tagged,
        identifiable_unique=len(by_article),
        ignored_zotero_only=len(set(by_article) - set(db_favorites)),
    )

    canonical_by_article = {}
    merged_by_key = {}
    trash_keys = set()

    for article_number, row in db_favorites.items():
        existing = by_article.get(article_number, [])
        if not existing:
            plan.creates.append(build_zotero_item(row, managed_tag, collection_key))
            continue

        canonical = choose_canonical(existing, managed_tag, collection_key)
        canonical_key = item_key(canonical)
        canonical_by_article[article_number] = canonical_key
        if not dedupe or len(existing) < 2:
            continue

        extras = [item for item in existing if item_key(item) != canonical_key]
        mismatches = []
        for extra in extras:
            key = item_key(extra)
            expected = int((extra.get("meta") or {}).get("numChildren") or 0)
            actual = len(children_by_parent.get(key, []))
            if expected != actual:
                mismatches.append({"key": key, "expected_children": expected, "loaded_children": actual})
        if mismatches:
            plan.unsafe_duplicates[article_number] = {"child_count_mismatches": mismatches}
            continue

        merged, conflicts = merge_duplicate_data(canonical, extras)
        merged_by_key[canonical_key] = merged
        if conflicts:
            plan.metadata_conflicts[article_number] = conflicts

        for extra in extras:
            extra_key = item_key(extra)
            for child in children_by_parent.get(extra_key, []):
                child_data = copy.deepcopy(child.get("data", child))
                child_data["parentItem"] = canonical_key
                plan.child_reparentings.append(child_data)
            trash_keys.add(extra_key)
            plan.trash_articles[extra_key] = article_number

    for article_number, items in by_article.items():
        desired_key = canonical_by_article.get(article_number)
        for item in items:
            key = item_key(item)
            if key in trash_keys:
                continue
            original = item.get("data", item)
            base = merged_by_key.get(key, original)
            should_manage = bool(desired_key and key == desired_key)
            updated = updated_managed_data(
                base,
                managed_tag,
                should_manage,
                collection_key,
                db_favorites.get(article_number),
            )

            before_tag = managed_tag in tag_names(original)
            after_tag = managed_tag in tag_names(updated)
            before_collection = bool(
                collection_key and collection_key in original.get("collections", [])
            )
            after_collection = bool(
                collection_key and collection_key in updated.get("collections", [])
            )
            if before_tag != after_tag:
                (plan.tag_additions if after_tag else plan.tag_removals).append(updated)
            if before_collection != after_collection:
                target = plan.collection_additions if after_collection else plan.collection_removals
                target.append(updated)
            if managed_citation_snapshot(original.get("extra")) != managed_citation_snapshot(
                updated.get("extra")
            ):
                plan.citation_updates.append(updated)
            if original.get("url") != updated.get("url"):
                plan.url_updates.append(updated)
            if updated != original:
                plan.item_updates.append(updated)

    plan.trash_keys = sorted(trash_keys)
    return plan


class ZoteroClient:
    def __init__(self, user_id, api_key, timeout=30):
        self.user_id = str(user_id)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Zotero-API-Key": api_key,
            "Zotero-API-Version": API_VERSION,
            "User-Agent": "IEEE-Paper-Server-Zotero-Sync/2.0",
        })
        self.backoff_until = 0.0

    def _request(self, method, path, **kwargs):
        delay = self.backoff_until - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        url = path if path.startswith("http") else API_BASE + path
        response = self.session.request(method, url, timeout=self.timeout, **kwargs)
        if response.headers.get("Backoff"):
            self.backoff_until = time.monotonic() + float(response.headers["Backoff"])
        if response.status_code in {429, 503}:
            retry_after = float(response.headers.get("Retry-After", "2"))
            time.sleep(retry_after)
            response = self.session.request(method, url, timeout=self.timeout, **kwargs)
        response.raise_for_status()
        return response

    def validate_write_access(self):
        response = self._request("GET", "/keys/current")
        data = response.json()
        access = (data.get("access") or {}).get("user") or {}
        if str(data.get("userID")) != self.user_id:
            raise RuntimeError("ZOTERO_USER_ID does not match the API key owner")
        if not access.get("library") or not access.get("write"):
            raise RuntimeError("Zotero API key needs personal-library read/write access")
        return access

    def _fetch_paginated(self, path, **params):
        objects = []
        start = 0
        total = None
        while total is None or start < total:
            query = {"format": "json", "limit": 100, "start": start, **params}
            response = self._request("GET", path, params=query)
            batch = response.json()
            if total is None:
                total = int(response.headers.get("Total-Results", "0") or 0)
            objects.extend(batch)
            if not batch or len(batch) < 100:
                break
            start += len(batch)
        return objects

    def fetch_top_items(self):
        return self._fetch_paginated(f"/users/{self.user_id}/items/top")

    def fetch_all_items(self):
        return self._fetch_paginated(f"/users/{self.user_id}/items")

    def fetch_collections(self):
        return self._fetch_paginated(f"/users/{self.user_id}/collections")

    def fetch_items_by_keys(self, keys):
        found = []
        keys = list(keys)
        for offset in range(0, len(keys), 50):
            batch = keys[offset:offset + 50]
            found.extend(self._fetch_paginated(
                f"/users/{self.user_id}/items", itemKey=",".join(batch)
            ))
        return found

    def write_objects(self, objects, batch_size=50):
        written = 0
        for offset in range(0, len(objects), batch_size):
            batch = objects[offset:offset + batch_size]
            response = self._request(
                "POST",
                f"/users/{self.user_id}/items",
                json=batch,
                headers={"Zotero-Write-Token": uuid.uuid4().hex},
            )
            result = response.json()
            failed = result.get("failed") or {}
            if failed:
                raise RuntimeError(f"Zotero batch write failed: {failed}")
            written += len(result.get("successful") or {})
            written += len(result.get("unchanged") or {})
        return written


def resolve_collection_key(collections, collection_name):
    matches = [
        collection for collection in collections
        if (collection.get("data", {}).get("name") or "").strip() == collection_name
    ]
    if not matches:
        raise RuntimeError(f"Zotero collection not found: {collection_name}")
    if len(matches) > 1:
        keys = ", ".join(item_key(collection) for collection in matches)
        raise RuntimeError(f"Multiple Zotero collections named {collection_name}: {keys}")
    return item_key(matches[0])


def children_index(items):
    result = defaultdict(list)
    for item in items:
        parent = item.get("data", {}).get("parentItem")
        if parent:
            result[parent].append(item)
    return result


def prepare_trash_updates(client, trash_keys):
    current = client.fetch_items_by_keys(trash_keys)
    by_key = {item_key(item): item for item in current}
    missing = sorted(set(trash_keys) - set(by_key))
    if missing:
        raise RuntimeError(f"Duplicate parents disappeared before trash step: {missing[:10]}")

    updates = []
    unsafe = []
    for key in trash_keys:
        item = by_key[key]
        child_count = int((item.get("meta") or {}).get("numChildren") or 0)
        if child_count:
            unsafe.append({"key": key, "remaining_children": child_count})
            continue
        data = copy.deepcopy(item.get("data", item))
        data["deleted"] = 1
        updates.append(data)
    if unsafe:
        raise RuntimeError(f"Duplicate parents still have children: {unsafe[:10]}")
    return updates


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)


def report_payload(plan, db_favorites, zotero_items, managed_tag, collection_name, collection_key, dedupe):
    return {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "dry-run",
        "managed_tag": managed_tag,
        "collection_name": collection_name,
        "collection_key": collection_key,
        "dedupe": dedupe,
        "summary": plan.summary(len(db_favorites), len(zotero_items)),
        "create_article_numbers": sorted(
            extract_article_number(item)
            or ARTICLE_EXTRA_RE.search(item.get("extra", "")).group(1)
            for item in plan.creates
        ),
        "update_item_keys": sorted(item.get("key", "") for item in plan.item_updates),
        "citation_update_item_keys": sorted(
            item.get("key", "") for item in plan.citation_updates
        ),
        "url_update_item_keys": sorted(
            item.get("key", "") for item in plan.url_updates
        ),
        "reparent_child_keys": sorted(item.get("key", "") for item in plan.child_reparentings),
        "trash_keys": plan.trash_keys,
        "trash_articles": plan.trash_articles,
        "duplicates": plan.duplicates,
        "unsafe_duplicates": plan.unsafe_duplicates,
        "metadata_conflicts": plan.metadata_conflicts,
        "unidentified_tagged_keys": sorted(plan.unidentified_tagged_keys),
    }


def snapshot_payload(
    db_favorites,
    zotero_items,
    all_items,
    managed_tag,
    collection_name,
    collection_key,
    plan,
):
    return {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "managed_tag": managed_tag,
        "collection_name": collection_name,
        "collection_key": collection_key,
        "db_favorites": list(db_favorites.values()),
        "zotero_top_items": zotero_items,
        "zotero_all_active_items": all_items,
        "planned_trash_keys": plan.trash_keys,
        "planned_reparent_child_keys": [
            item.get("key", "") for item in plan.child_reparentings
        ],
    }


def active_top_items(all_items):
    return [item for item in all_items if not item.get("data", {}).get("parentItem")]


def main():
    parser = argparse.ArgumentParser(description="MySQL 즐겨찾기 -> Zotero 단방향 동기화")
    parser.add_argument("--apply", action="store_true", help="실제 Zotero 변경 적용(기본은 dry-run)")
    parser.add_argument("--dedupe", action="store_true", help="자식을 보존하며 동일 논문 중복 부모를 휴지통으로 이동")
    parser.add_argument("--tag", default=DEFAULT_TAG, help=f"관리 태그(기본: {DEFAULT_TAG})")
    parser.add_argument("--collection", default=DEFAULT_COLLECTION, help=f"동기화 컬렉션(기본: {DEFAULT_COLLECTION})")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument(
        "--max-changes", type=int, default=2000,
        help="예상 변경이 많으면 안전 중단(기본 2000)",
    )
    args = parser.parse_args()

    user_id = (os.getenv("ZOTERO_USER_ID") or "").strip()
    api_key = (os.getenv("ZOTERO_API_KEY") or "").strip()
    if not user_id or not api_key:
        raise SystemExit("[오류] .env에 ZOTERO_USER_ID와 ZOTERO_API_KEY가 필요합니다.")
    if not 1 <= args.batch_size <= 50:
        raise SystemExit("[오류] --batch-size는 1~50이어야 합니다.")

    client = ZoteroClient(user_id, api_key, timeout=60)
    client.validate_write_access()
    collections = client.fetch_collections()
    collection_key = resolve_collection_key(collections, args.collection)
    db_favorites = fetch_db_favorites()
    if not db_favorites:
        raise SystemExit("[안전 중단] DB 즐겨찾기가 0건입니다.")

    if args.dedupe:
        print("[조회] 중복 자식 보존을 위해 Zotero 활성 항목 전체를 불러옵니다.", flush=True)
        all_items = client.fetch_all_items()
        zotero_items = active_top_items(all_items)
        indexed_children = children_index(all_items)
    else:
        all_items = None
        zotero_items = client.fetch_top_items()
        indexed_children = {}
    if not zotero_items:
        raise SystemExit("[안전 중단] Zotero 상위 항목이 0건입니다.")

    plan = build_sync_plan(
        db_favorites,
        zotero_items,
        args.tag,
        collection_key,
        dedupe=args.dedupe,
        children_by_parent=indexed_children,
    )
    summary = plan.summary(len(db_favorites), len(zotero_items))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    plan_path = REPORT_DIR / f"zotero_sync_plan_{stamp}.json"
    write_json(
        plan_path,
        report_payload(
            plan, db_favorites, zotero_items, args.tag,
            args.collection, collection_key, args.dedupe,
        ),
    )

    print("=" * 64)
    print("MySQL -> Zotero 즐겨찾기·컬렉션 단방향 동기화")
    print("=" * 64)
    print(f"collection: {args.collection} ({collection_key})")
    for key, value in summary.items():
        print(f"{key}: {value}")
    print(f"plan: {plan_path}")

    if plan.unsafe_duplicates:
        raise SystemExit(
            f"[안전 중단] 자식 수가 일치하지 않는 중복 그룹이 "
            f"{len(plan.unsafe_duplicates)}개입니다. 계획 JSON을 확인하세요."
        )
    if not args.apply:
        print("[DRY-RUN] Zotero를 변경하지 않았습니다. 실제 반영: --apply")
        return
    if plan.mutation_count > args.max_changes:
        raise SystemExit(
            f"[안전 중단] 변경 {plan.mutation_count}건이 --max-changes "
            f"{args.max_changes}를 초과합니다."
        )
    if plan.mutation_count == 0:
        print("[완료] Zotero 태그·컬렉션이 DB 실PDF 보유 항목과 이미 일치합니다.")
        return

    if all_items is None:
        # 평상시 동기화는 상위 항목만 변경하므로 그 범위만 백업한다.
        all_items = zotero_items
    backup_path = REPORT_DIR / f"zotero_sync_backup_{stamp}.json"
    write_json(
        backup_path,
        snapshot_payload(
            db_favorites, zotero_items, all_items, args.tag,
            args.collection, collection_key, plan,
        ),
    )
    print(f"backup: {backup_path}", flush=True)

    updated = client.write_objects(plan.item_updates, batch_size=args.batch_size)
    created = client.write_objects(plan.creates, batch_size=args.batch_size)
    print(f"[적용] 논문 갱신 {updated}건 / 신규 생성 {created}건", flush=True)

    reparented = client.write_objects(
        plan.child_reparentings, batch_size=args.batch_size
    )
    print(f"[적용] 중복 부모의 자식 재연결 {reparented}건", flush=True)

    trashed = 0
    if plan.trash_keys:
        trash_updates = prepare_trash_updates(client, plan.trash_keys)
        trashed = client.write_objects(trash_updates, batch_size=args.batch_size)
    print(f"[적용] 중복 부모 휴지통 이동 {trashed}건", flush=True)

    refreshed_items = client.fetch_top_items()
    remaining = build_sync_plan(
        db_favorites,
        refreshed_items,
        args.tag,
        collection_key,
        dedupe=args.dedupe,
        children_by_parent={},
    )
    remaining_summary = remaining.summary(len(db_favorites), len(refreshed_items))
    managed_count = sum(
        args.tag in tag_names(item)
        for item in refreshed_items
        if item.get("data", {}).get("itemType") in PAPER_TYPES
    )
    collection_count = sum(
        collection_key in item.get("data", {}).get("collections", [])
        for item in refreshed_items
    )
    verify_path = REPORT_DIR / f"zotero_sync_verify_{stamp}.json"
    write_json(verify_path, {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "managed_tag": args.tag,
        "collection_name": args.collection,
        "collection_key": collection_key,
        "top_items": len(refreshed_items),
        "managed_paper_items": managed_count,
        "target_collection_items": collection_count,
        "summary": remaining_summary,
    })
    print(f"verify: {verify_path}")
    if remaining.mutation_count:
        raise SystemExit(
            f"[경고] 재검증 후 미반영 변경이 {remaining.mutation_count}건 남았습니다."
        )
    if managed_count != len(db_favorites) or collection_count != len(db_favorites):
        raise SystemExit(
            "[경고] DB 즐겨찾기 수와 Zotero 관리 태그/컬렉션 수가 일치하지 않습니다."
        )
    print("[완료] Zotero 태그·컬렉션·중복 상태가 DB 원본을 반영했습니다.")


if __name__ == "__main__":
    main()
