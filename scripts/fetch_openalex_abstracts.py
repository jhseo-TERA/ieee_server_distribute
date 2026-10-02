# -*- coding: utf-8 -*-
r"""Enrich Core/Adjacent SerDes papers from the CC0 OpenAlex Works API.

This is the automated fallback for papers whose official IEEE metadata
abstract is not already cached. It never automates IEEE Xplore pages. DOI
lookups are batched (up to 100 per request), validated against the local title
and year, checkpointed in SQL, and saved as auditable UTF-8 text records.

Example::

    .venv\Scripts\python.exe scripts\fetch_openalex_abstracts.py --limit 1000
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from serdes_metrics import plain_text, title_similarity  # noqa: E402
from scripts.serdes_data_pipeline import (  # noqa: E402
    SERDES_LATEST_COMPLETE_RUNS_SQL,
    classify_link_media,
    classify_link_subtypes,
    classify_measurement_scopes,
    db_connect,
    ensure_schema,
    extract_abstracts,
    store_abstract_snapshot,
)


OPENALEX_WORKS_URL = "https://api.openalex.org/works"
OPENALEX_PROVIDER = "openalex"
DEFAULT_CACHE_DIR = ROOT / "py_01_data" / "openalex_abstracts"
DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.I)
SAFE_PATH_RE = re.compile(r"[^A-Za-z0-9._-]+")


class OpenAlexError(RuntimeError):
    pass


class OpenAlexRateLimited(OpenAlexError):
    pass


def normalize_doi(value: str | None) -> str:
    doi = DOI_PREFIX_RE.sub("", plain_text(value)).strip().lower()
    return doi.rstrip(".,;")


def reconstruct_abstract(index: dict[str, list[int]] | None) -> str:
    """Reconstruct OpenAlex's documented abstract inverted index."""
    if not index:
        return ""
    positioned: dict[int, str] = {}
    for token, positions in index.items():
        if not isinstance(token, str) or not isinstance(positions, list):
            raise OpenAlexError("invalid abstract_inverted_index")
        for position in positions:
            if not isinstance(position, int) or position < 0:
                raise OpenAlexError("invalid abstract token position")
            existing = positioned.get(position)
            if existing is not None and existing != token:
                raise OpenAlexError(f"conflicting abstract token at position {position}")
            positioned[position] = token
    if not positioned:
        return ""
    expected = list(range(max(positioned) + 1))
    if sorted(positioned) != expected:
        raise OpenAlexError("non-contiguous abstract token positions")
    return plain_text(" ".join(positioned[position] for position in expected))


def work_to_article(work: dict[str, Any], paper: dict[str, Any]) -> dict[str, str]:
    expected_doi = normalize_doi(paper.get("doi"))
    actual_doi = normalize_doi(work.get("doi"))
    if not expected_doi or actual_doi != expected_doi:
        raise OpenAlexError(
            f"DOI mismatch for paper {paper.get('id')}: {expected_doi} != {actual_doi}"
        )
    title = plain_text(work.get("title") or work.get("display_name"))
    similarity = title_similarity(title, paper.get("title"))
    if similarity < 0.82:
        raise OpenAlexError(
            f"title mismatch for DOI {expected_doi} (similarity={similarity:.3f})"
        )
    try:
        expected_year = int(paper.get("year") or 0)
        actual_year = int(work.get("publication_year") or 0)
    except (TypeError, ValueError):
        expected_year = actual_year = 0
    if expected_year and actual_year and abs(expected_year - actual_year) > 1:
        raise OpenAlexError(
            f"year mismatch for DOI {expected_doi}: {expected_year} != {actual_year}"
        )
    openalex_id = plain_text(work.get("id"))
    return {
        "abstract": reconstruct_abstract(work.get("abstract_inverted_index")),
        "abstract_url": openalex_id,
        "provider_record_id": openalex_id.rsplit("/", 1)[-1] or openalex_id,
        "title": title,
        "doi": actual_doi,
    }


class OpenAlexClient:
    def __init__(self, api_key: str | None = None, timeout: float = 45.0):
        self.api_key = plain_text(api_key)
        self.timeout = max(5.0, float(timeout))
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "IEEE-Paper-Server-SerDes/1.0 (OpenAlex CC0 metadata)",
            "Accept": "application/json",
        })

    def fetch_by_dois(self, dois: list[str]) -> list[dict[str, Any]]:
        normalized = [normalize_doi(doi) for doi in dois if normalize_doi(doi)]
        if not normalized:
            return []
        if len(normalized) > 100:
            raise ValueError("OpenAlex accepts at most 100 OR values per request")
        params = {
            "filter": "doi:" + "|".join(
                f"https://doi.org/{doi}" for doi in normalized
            ),
            "per_page": len(normalized),
            "select": (
                "id,doi,title,display_name,publication_year,"
                "abstract_inverted_index"
            ),
        }
        if self.api_key:
            params["api_key"] = self.api_key
        try:
            response = self.session.get(
                OPENALEX_WORKS_URL, params=params, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise OpenAlexError(f"OpenAlex request failed: {exc}") from None
        if response.status_code == 429:
            raise OpenAlexRateLimited("OpenAlex daily/rate budget reached (HTTP 429)")
        if response.status_code in {401, 403}:
            raise OpenAlexError(f"OpenAlex authorization failed (HTTP {response.status_code})")
        if response.status_code >= 500:
            raise OpenAlexRateLimited(f"OpenAlex server unavailable (HTTP {response.status_code})")
        if response.status_code != 200:
            raise OpenAlexError(f"OpenAlex HTTP {response.status_code}: {response.text[:200]}")
        payload = response.json()
        return list(payload.get("results") or [])


def upsert_fetch_state(
    cur,
    paper_id: int,
    status: str,
    detail: str | None = None,
) -> None:
    attempted_at = datetime.now(timezone.utc).replace(tzinfo=None)
    completed_at = (
        attempted_at if status in {"abstract", "no_abstract", "not_found"} else None
    )
    cur.execute(
        """
        INSERT INTO paper_metadata_fetch_state
            (paper_id, provider, fetch_status, attempt_count, detail,
             last_attempt_at, completed_at)
        VALUES (%s, %s, %s, 1, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            fetch_status=VALUES(fetch_status), attempt_count=attempt_count+1,
            detail=VALUES(detail), last_attempt_at=VALUES(last_attempt_at),
            completed_at=VALUES(completed_at)
        """,
        (
            paper_id,
            OPENALEX_PROVIDER,
            status,
            (detail or "")[:500] or None,
            attempted_at,
            completed_at,
        ),
    )


def select_targets(
    conn,
    limit: int,
    venue: str | None = None,
    retry_terminal: bool = False,
) -> list[dict[str, Any]]:
    venue_sql = "AND p.source_name=%s" if venue else ""
    terminal_sql = "" if retry_terminal else (
        "AND (fs.fetch_status IS NULL OR "
        "fs.fetch_status NOT IN ('abstract','no_abstract','not_found'))"
    )
    params: list[Any] = [OPENALEX_PROVIDER]
    if venue:
        params.append(venue)
    params.append(max(1, int(limit)))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT p.id, p.article_number, p.title, p.authors, p.year,
                   p.source_name, p.doi, p.url, s.relevance_class,
                   s.relevance_score
            FROM serdes_paper_screenings s
            JOIN papers p ON p.id=s.paper_id
            LEFT JOIN paper_current_abstracts a ON a.paper_id=p.id
            LEFT JOIN paper_metadata_fetch_state fs
              ON fs.paper_id=p.id AND fs.provider=%s
            WHERE p.source_system='ieee' AND s.include_in_survey=1
              AND s.run_id IN ({SERDES_LATEST_COMPLETE_RUNS_SQL})
              AND a.id IS NULL
              AND NULLIF(TRIM(p.doi), '') IS NOT NULL
              {venue_sql}
              {terminal_sql}
            ORDER BY s.relevance_score DESC, CAST(p.year AS UNSIGNED) DESC,
                     p.source_name, p.id
            LIMIT %s
            """,
            tuple(params),
        )
        return list(cur.fetchall())


def _safe_path_part(value: str | None, fallback: str) -> str:
    cleaned = SAFE_PATH_RE.sub("-", plain_text(value)).strip("-._")
    return cleaned[:80] or fallback


def write_text_cache(
    cache_dir: Path,
    paper: dict[str, Any],
    article: dict[str, str],
) -> Path:
    venue = _safe_path_part(paper.get("source_name"), "unknown-venue")
    target_dir = cache_dir / venue
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{paper['article_number']}.txt"
    body = (
        f"IEEE Article Number: {paper['article_number']}\n"
        f"DOI: {article.get('doi') or paper.get('doi') or ''}\n"
        f"Title: {article.get('title') or paper.get('title') or ''}\n"
        f"Venue: {paper.get('source_name') or ''}\n"
        f"Year: {paper.get('year') or ''}\n"
        "Provider: OpenAlex scholarly metadata (CC0)\n"
        f"Source: {article.get('abstract_url') or ''}\n"
        f"Retrieved UTC: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        "\n--- ABSTRACT ---\n"
        f"{article.get('abstract') or ''}\n"
    )
    temporary = target.with_suffix(".txt.part")
    temporary.write_text(body, encoding="utf-8")
    os.replace(temporary, target)
    return target


def fetch_openalex(
    conn,
    limit: int,
    batch_size: int,
    delay: float,
    cache_dir: Path,
    venue: str | None = None,
    retry_terminal: bool = False,
    api_key: str | None = None,
) -> dict[str, Any]:
    targets = select_targets(conn, limit, venue, retry_terminal)
    client = OpenAlexClient(api_key=api_key)
    stats: dict[str, Any] = {
        "targeted": len(targets),
        "batches": 0,
        "fetched": 0,
        "new_revisions": 0,
        "no_abstract": 0,
        "not_found": 0,
        "mismatch": 0,
        "blocked": False,
        "cache_dir": str(cache_dir.resolve()),
    }
    width = min(100, max(1, int(batch_size)))
    for start in range(0, len(targets), width):
        batch = targets[start:start + width]
        try:
            works = client.fetch_by_dois([paper["doi"] for paper in batch])
        except OpenAlexRateLimited as exc:
            with conn.cursor() as cur:
                for paper in batch:
                    upsert_fetch_state(cur, paper["id"], "blocked", str(exc))
            conn.commit()
            stats["blocked"] = True
            print(f"  stopped: {exc}")
            break
        by_doi = {
            normalize_doi(work.get("doi")): work
            for work in works
            if normalize_doi(work.get("doi"))
        }
        with conn.cursor() as cur:
            for paper in batch:
                doi = normalize_doi(paper.get("doi"))
                work = by_doi.get(doi)
                if not work:
                    upsert_fetch_state(cur, paper["id"], "not_found")
                    stats["not_found"] += 1
                    continue
                try:
                    article = work_to_article(work, paper)
                except OpenAlexError as exc:
                    upsert_fetch_state(cur, paper["id"], "mismatch", str(exc))
                    stats["mismatch"] += 1
                    continue
                if not article["abstract"]:
                    upsert_fetch_state(cur, paper["id"], "no_abstract")
                    stats["no_abstract"] += 1
                    continue
                _, created = store_abstract_snapshot(
                    cur, paper, article, provider=OPENALEX_PROVIDER
                )
                path = write_text_cache(cache_dir, paper, article)
                upsert_fetch_state(cur, paper["id"], "abstract", f"cached={path}")
                stats["fetched"] += 1
                stats["new_revisions"] += int(created)
        conn.commit()
        stats["batches"] += 1
        completed = min(start + len(batch), len(targets))
        print(
            f"  OpenAlex DOI {completed:,}/{len(targets):,} "
            f"(saved={stats['fetched']:,}, no_abstract={stats['no_abstract']:,}, "
            f"not_found={stats['not_found']:,}, mismatch={stats['mismatch']:,})"
        )
        if completed < len(targets):
            time.sleep(max(0.5, float(delay)))
    return stats


def refresh_serdes_enrichment(conn, venue: str | None = None) -> dict[str, Any]:
    """Extract every current included abstract, then rebuild derived taxonomy.

    This is intentionally independent of ``new_revisions``.  A previous run
    may have committed an abstract snapshot and then failed during extraction
    or classification; rerunning the fetcher must repair that state even when
    OpenAlex has no new revision to return.
    """
    return {
        "abstract_extraction": extract_abstracts(
            conn,
            venue=venue,
            screened_only=True,
        ),
        "link_media": classify_link_media(conn, included_only=True),
        "energy_scopes": classify_measurement_scopes(conn),
        "link_subtypes": classify_link_subtypes(conn, included_only=True),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--venue")
    parser.add_argument("--retry-terminal", action="store_true")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.limit < 1:
        raise SystemExit("--limit must be at least 1")
    cache_dir = args.cache_dir
    if not cache_dir.is_absolute():
        cache_dir = ROOT / cache_dir
    conn = db_connect()
    try:
        print(f"Schema: {ensure_schema(conn)} idempotent statements applied")
        result = fetch_openalex(
            conn,
            limit=args.limit,
            batch_size=args.batch_size,
            delay=args.delay,
            cache_dir=cache_dir,
            venue=args.venue,
            retry_terminal=args.retry_terminal,
            api_key=os.getenv("OPENALEX_API_KEY"),
        )
        print("OpenAlex abstracts:", json.dumps(result, ensure_ascii=False))
        refreshed = refresh_serdes_enrichment(conn, venue=args.venue)
        for label, key in (
            ("Abstract extraction", "abstract_extraction"),
            ("Link media", "link_media"),
            ("Energy scopes", "energy_scopes"),
            ("Link subtypes", "link_subtypes"),
        ):
            print(label + ":", json.dumps(refreshed[key], ensure_ascii=False))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
