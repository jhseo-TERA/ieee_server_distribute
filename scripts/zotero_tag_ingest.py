# -*- coding: utf-8 -*-
"""
Zotero에서 특정 태그(기본 "📌")가 붙은 논문을 자동으로 찾아 옵시디언 노트로
만든다 — 예약 작업(cron)으로 주기 실행해서 "태그만 붙이면 알아서 노트가
생긴다"를 구현하기 위한 스크립트.

zotero-cli 대신 Zotero 로컬 HTTP API(포트 23119)를 직접 호출한다 — zotero-cli의
출력은 사람이 읽기 위한 마크다운 텍스트라 자동화 파싱이 불안정하고, 로컬 API가
주는 JSON이 훨씬 안전하다.

로컬 API는 읽기 전용(쓰기는 ZOTERO_API_KEY 필요)이라, 태그를 지우거나 표시할
수 없다. 대신 이 스크립트가 직접 상태 파일(scripts/.zotero_tag_ingest_state.json)에
"어떤 항목을 언제(dateModified 기준) 처리했는지" 기록해서 중복 처리를 막는다.
dateModified가 마지막 처리 시점보다 최신이면(예: 나중에 하이라이트를 추가한
경우) 다시 갱신 대상으로 잡는다 — 태그를 영구히 남겨둬도 무방한 설계.

사용:
  .venv\\Scripts\\python.exe scripts\\zotero_tag_ingest.py            # 실행
  .venv\\Scripts\\python.exe scripts\\zotero_tag_ingest.py --tag 📌   # 태그 지정
  .venv\\Scripts\\python.exe scripts\\zotero_tag_ingest.py --dry-run  # 미리보기만
"""
import os
import re
import sys
import json
import hashlib
import argparse
import subprocess

import requests
from dotenv import load_dotenv

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAULT_PAPERS = os.path.join(ROOT, "Obsidian", "IEEE•Optica•Nature", "papers")
STATE_FILE = os.path.join(ROOT, "scripts", ".zotero_tag_ingest_state.json")
RELATED_SCRIPT = os.path.join(ROOT, "scripts", "related_papers.py")
PYTHON = os.path.join(ROOT, ".venv", "Scripts", "python.exe")

load_dotenv(os.path.join(ROOT, ".env"))
LOCAL_ZOTERO_BASE = "http://127.0.0.1:23119/api/users/0"
ZOTERO_BASE = None
ZOTERO_HEADERS = {}
DEFAULT_TAG = "\U0001F4CC"  # 📌
PAPER_TYPES = {"journalArticle", "conferencePaper"}

INVALID_FILENAME_RE = re.compile(r'[\\/:*?"<>|]')


def load_state():
    if os.path.isfile(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def configure_zotero_api():
    global ZOTERO_BASE, ZOTERO_HEADERS
    if ZOTERO_BASE:
        return
    try:
        response = requests.get(
            f"{LOCAL_ZOTERO_BASE}/items", params={"limit": 1}, timeout=5
        )
        response.raise_for_status()
        ZOTERO_BASE = LOCAL_ZOTERO_BASE
        ZOTERO_HEADERS = {}
        print("[*] Zotero Local API 사용")
        return
    except requests.exceptions.RequestException:
        pass

    user_id = os.getenv("ZOTERO_USER_ID", "").strip()
    api_key = os.getenv("ZOTERO_API_KEY", "").strip()
    if not user_id or not api_key:
        raise requests.exceptions.ConnectionError(
            "Zotero Local API unavailable and Web API credentials are missing"
        )
    ZOTERO_BASE = f"https://api.zotero.org/users/{user_id}"
    ZOTERO_HEADERS = {
        "Zotero-API-Key": api_key,
        "Zotero-API-Version": "3",
    }
    response = requests.get(
        f"{ZOTERO_BASE}/items",
        params={"limit": 1, "format": "json"},
        headers=ZOTERO_HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    print("[*] Zotero Web API read-only fallback 사용")


def zotero_get(path, **params):
    configure_zotero_api()
    response = requests.get(
        f"{ZOTERO_BASE}{path}",
        params=params,
        headers=ZOTERO_HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    return response


def zotero_get_all(path, **params):
    """Fetch every Zotero result page instead of silently stopping at 100."""
    items = []
    start = 0
    while True:
        response = zotero_get(path, **params, limit=100, start=start)
        batch = response.json()
        items.extend(batch)
        total = int(response.headers.get("Total-Results", "0") or 0)
        if not batch or len(batch) < 100 or (total and len(items) >= total):
            break
        start += len(batch)
    return items


def get_tagged_items(tag):
    return [
        item for item in zotero_get_all("/items", tag=tag, format="json")
        if item["data"].get("itemType") in PAPER_TYPES
    ]


def get_children(key):
    return zotero_get_all(f"/items/{key}/children", format="json")


def derive_publisher(url):
    if "ieeexplore.ieee.org" in url:
        return "IEEE"
    if "opg.optica.org" in url:
        return "OPTICA"
    if "nature.com" in url:
        return "NATURE"
    return "UNKNOWN"


def derive_article_number(url, publisher):
    if publisher == "IEEE":
        m = re.search(r"/document/([^/?]+)", url)
        return m.group(1) if m else None
    if publisher == "NATURE":
        m = re.search(r"/articles/([^/?]+)", url)
        return m.group(1) if m else None
    if publisher == "OPTICA":
        m = re.search(r"[?&]uri=([^&]+)", url)
        return m.group(1) if m else None
    return None


def sanitize_filename(title, maxlen=150):
    name = INVALID_FILENAME_RE.sub("-", title).strip()
    return name[:maxlen]


def get_related_papers(title, article_number):
    args = [PYTHON, RELATED_SCRIPT, "--title", title, "--limit", "5"]
    if article_number:
        args += ["--exclude", str(article_number)]
    try:
        out = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=30)
        return out.stdout.strip() or "_(관련 논문 없음)_"
    except Exception as e:
        return f"_(관련 논문 조회 실패: {e})_"


def attachment_tags(child):
    return {
        tag.get("tag") for tag in child.get("data", {}).get("tags", [])
        if tag.get("tag")
    }


def select_pdf_attachment(children):
    """Prefer the canonical stored PDF, then another stored PDF, then linked."""
    candidates = []
    for child in children:
        data = child.get("data", {})
        if data.get("itemType") != "attachment":
            continue
        if data.get("contentType") != "application/pdf" or data.get("deleted"):
            continue
        tags = attachment_tags(child)
        mode = data.get("linkMode")
        uploaded = bool(
            data.get("md5") or child.get("links", {}).get("enclosure")
        )
        priority = (
            0 if "PaperServerCanonicalPDF" in tags else
            1 if mode == "imported_file" and uploaded else
            2 if mode == "imported_file" else
            3 if "PaperServerLinkedPDF" in tags else
            4
        )
        candidates.append((priority, str(data.get("dateAdded") or ""), child))
    if not candidates:
        return None
    candidates.sort(key=lambda entry: (entry[0], entry[1], entry[2].get("key", "")))
    return candidates[0][2]


def pdf_link_markdown(children):
    child = select_pdf_attachment(children)
    if not child:
        return "_(PDF 없음)_"
    data = child.get("data", {})
    key = child.get("key") or data.get("key")
    filename = data.get("filename")
    if not filename and data.get("path"):
        filename = os.path.basename(data["path"])
    filename = filename or data.get("title") or "attachment.pdf"
    if key:
        return f"[{filename}](zotero://open-pdf/library/items/{key})"
    href = child.get("links", {}).get("enclosure", {}).get("href")
    return f"[{filename}]({href})" if href else "_(PDF 없음)_"


def item_fingerprint(item, children):
    """Track child changes as well as the parent item's dateModified value."""
    data = item.get("data", {})
    child_state = []
    for child in children:
        cdata = child.get("data", {})
        child_state.append({
            "key": child.get("key") or cdata.get("key"),
            "version": child.get("version") or cdata.get("version"),
            "dateModified": cdata.get("dateModified"),
            "itemType": cdata.get("itemType"),
            "linkMode": cdata.get("linkMode"),
            "filename": cdata.get("filename"),
            "path": cdata.get("path"),
        })
    payload = {
        "key": item.get("key") or data.get("key"),
        "version": item.get("version") or data.get("version"),
        "dateModified": data.get("dateModified"),
        "children": sorted(child_state, key=lambda entry: str(entry["key"] or "")),
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_note_body(data, publisher, article_number, children, related_md):
    title = data.get("title", "Untitled")
    authors = [c.get("name") or f"{c.get('firstName','')} {c.get('lastName','')}".strip()
               for c in data.get("creators", [])]
    year = (data.get("date") or "")[:4]
    venue = data.get("publicationTitle") or data.get("conferenceName") or data.get("proceedingsTitle") or ""
    url = data.get("url", "")
    key = data["key"]

    pdf_link = pdf_link_markdown(children)

    notes_lines = []
    for child in children:
        cdata = child.get("data", {})
        if cdata.get("itemType") != "note":
            continue
        raw = re.sub(r"<[^>]+>", "", cdata.get("note", "")).strip()
        # RIS import 시 우리가 넣어둔 "Page: p.x" / "Issue: Issue x" 같은 스텁은 실질 내용이 아니므로 skip
        if re.fullmatch(r"(Page|Issue):\s*.+", raw):
            continue
        if raw:
            notes_lines.append(raw)
    notes_section = "\n\n".join(notes_lines) if notes_lines else "_(no highlights yet)_"

    authors_str = ", ".join(authors)
    tags_list = [t["tag"] for t in data.get("tags", []) if t.get("tag") != DEFAULT_TAG]

    title_yaml = json.dumps(title, ensure_ascii=False)
    authors_yaml = json.dumps(authors, ensure_ascii=False)
    venue_yaml = json.dumps(venue, ensure_ascii=False)
    tags_yaml = json.dumps(tags_list, ensure_ascii=False)
    body = f"""---
title: {title_yaml}
authors: {authors_yaml}
year: {year}
venue: {venue_yaml}
zotero-key: {key}
publisher: {publisher}
tags: {tags_yaml}
---

# {title}

**Authors:** {authors_str}
**Venue:** {venue}, {year}
**Source:** [{url}]({url})
**PDF:** {pdf_link}

## Notes / Highlights

{notes_section}

## Related Papers

{related_md}
"""
    return body


def main():
    ap = argparse.ArgumentParser(description="Zotero 태그 -> 옵시디언 노트 자동 ingest")
    ap.add_argument("--tag", default=DEFAULT_TAG, help="트리거로 쓸 태그 (기본: 📌)")
    ap.add_argument("--dry-run", action="store_true", help="실제로 노트를 쓰지 않고 대상만 표시")
    args = ap.parse_args()

    os.makedirs(VAULT_PAPERS, exist_ok=True)
    state = load_state()

    try:
        items = get_tagged_items(args.tag)
    except requests.exceptions.RequestException as exc:
        print("[!] Zotero Local/Web API에 연결할 수 없습니다: "
              f"{type(exc).__name__}")
        sys.exit(1)

    todo = []
    for it in items:
        key = it["key"]
        title = it["data"].get("title", "Untitled")
        modified = it["data"].get("dateModified", "")
        children = get_children(key)
        fingerprint = item_fingerprint(it, children)
        previous = state.get(key, {})
        expected_note = os.path.join(
            VAULT_PAPERS, sanitize_filename(title) + ".md"
        )
        if (
            previous.get("fingerprint") != fingerprint
            or not os.path.isfile(expected_note)
        ):
            todo.append((it, children, fingerprint))

    print(f"[*] 태그 '{args.tag}' 붙은 논문 {len(items)}건 중 신규/갱신 대상 {len(todo)}건")
    if not todo:
        return

    for it, children, fingerprint in todo:
        data = it["data"]
        key = it["key"]
        title = data.get("title", "Untitled")
        url = data.get("url", "")
        publisher = derive_publisher(url)
        article_number = derive_article_number(url, publisher)

        print(f"  - {title[:70]} [{key}] ...", end=" ")
        if args.dry_run:
            print("(dry-run, 건너뜀)")
            continue

        related_md = get_related_papers(title, article_number)
        body = build_note_body(data, publisher, article_number, children, related_md)

        fname = sanitize_filename(title) + ".md"
        out_path = os.path.join(VAULT_PAPERS, fname)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(body)

        state[key] = {
            "dateModified": data.get("dateModified", ""),
            "fingerprint": fingerprint,
            "title": title,
        }
        print("OK")

    if args.dry_run:
        print(f"[*] 미리보기만 했습니다({len(todo)}건 대상 확인) - 실제 반영하려면 --dry-run 없이 실행하세요.")
        return

    save_state(state)
    print(f"[완료] {len(todo)}건 처리, 상태 파일 갱신함 ({STATE_FILE})")


if __name__ == "__main__":
    main()
