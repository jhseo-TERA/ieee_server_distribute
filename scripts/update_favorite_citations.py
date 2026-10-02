#!/usr/bin/env python3
"""Update citation counts for favorite papers only.

Providers:
  * IEEE papers: IEEE Xplore Metadata API ``citing_paper_count``
  * Optica/Nature papers: Crossref ``is-referenced-by-count``

The job is resumable. Each successful lookup is committed in small batches and
only stale or missing favorite rows are selected on the next run. With account
isolation, any account's favorite can be refreshed and shared citations persist.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from html import unescape
from pathlib import Path
from urllib.parse import quote

import pymysql
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
META_DIR = ROOT / "py_01_data" / "00_metadata"
REPORT_DIR = ROOT / "py_02_reports"
sys.path.insert(0, str(ROOT))

from scripts.import_excel_to_db import extract_key, jlt_doi_key  # noqa: E402

load_dotenv(ROOT / ".env")

IEEE_API_URL = "https://ieeexploreapi.ieee.org/api/v1/search/articles"
CROSSREF_API_BASE = "https://api.crossref.org/works"

COLUMN_DDL = {
    "doi": "VARCHAR(255) NULL",
    "citation_count": "INT UNSIGNED NULL",
    "citation_source": "VARCHAR(20) NULL",
    "citation_updated_at": "DATETIME NULL",
}

LEADING_SESSION_RE = re.compile(
    r"^\s*(?:[A-Z]?\d+(?:\.\d+){1,3}\s*:?\s+)", re.IGNORECASE
)
NON_WORD_RE = re.compile(r"[^a-z0-9]+")
HTML_TAG_RE = re.compile(r"<[^>]+>")
OPTICA_PAGE_KEY_RE = re.compile(
    r"^oe-(?P<volume>\d+)-(?P<issue>\d+)-(?P<page>\d+)$", re.IGNORECASE
)


class CitationError(RuntimeError):
    pass


class ProviderQuotaError(CitationError):
    pass


@dataclass(frozen=True)
class CitationResult:
    count: int
    source: str
    doi: str | None = None


def db_connect():
    return pymysql.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", ""),
        database=os.getenv("DB_NAME", "ieee_repo"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def ensure_schema(conn) -> list[str]:
    """Add citation columns/indexes when missing and return added object names."""
    added = []
    with conn.cursor() as cur:
        cur.execute("SHOW COLUMNS FROM papers")
        existing_columns = {row["Field"] for row in cur.fetchall()}
        clauses = []
        for name, ddl in COLUMN_DDL.items():
            if name not in existing_columns:
                clauses.append(f"ADD COLUMN `{name}` {ddl}")
                added.append(name)

        cur.execute("SHOW INDEX FROM papers")
        existing_indexes = {row["Key_name"] for row in cur.fetchall()}
        if "idx_doi" not in existing_indexes:
            clauses.append("ADD INDEX idx_doi (doi)")
            added.append("idx_doi")
        if "idx_citation_count" not in existing_indexes:
            clauses.append("ADD INDEX idx_citation_count (citation_count)")
            added.append("idx_citation_count")
        if clauses:
            cur.execute("ALTER TABLE papers " + ", ".join(clauses))
    conn.commit()
    return added


def normalize_doi(value) -> str | None:
    text = str(value or "").strip().lower()
    if not text or text == "nan":
        return None
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text)
    return text if text.startswith("10.") and "/" in text else None


def cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def normalize_title(value: str) -> str:
    text = unicodedata.normalize("NFKC", unescape(value or ""))
    text = HTML_TAG_RE.sub("", text)
    text = LEADING_SESSION_RE.sub("", text)
    text = (
        text.replace("\u2010", "-")
        .replace("\u2011", "-")
        .replace("\u2012", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .lower()
    )
    return " ".join(NON_WORD_RE.sub(" ", text).split())


def derive_doi(paper: dict) -> str | None:
    """Derive identifiers only for publisher key formats with exact rules."""
    key = str(paper.get("article_number") or "").strip().lower()
    if paper.get("source_system") == "nature" and (
        key.startswith("s") or key.startswith("nphoton.")
        or re.fullmatch(r"ncomms\d+", key)
    ):
        return f"10.1038/{key.lower()}"
    if paper.get("source_system") == "optica":
        match = OPTICA_PAGE_KEY_RE.match(key)
        if match:
            try:
                year = int(paper.get("year") or 0)
            except (TypeError, ValueError):
                year = 0
            if year and year <= 2010:
                prefix = "OPEX" if year <= 2005 else "OE"
                return (
                    f"10.1364/{prefix}.{int(match.group('volume'))}."
                    f"{int(match.group('page')):06d}"
                ).lower()
    return None


def title_similarity(left: str, right: str) -> tuple[float, float]:
    a = normalize_title(left)
    b = normalize_title(right)
    if not a or not b:
        return 0.0, 0.0
    sequence = SequenceMatcher(None, a, b).ratio()
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    union = a_tokens | b_tokens
    jaccard = len(a_tokens & b_tokens) / len(union) if union else 0.0
    return sequence, jaccard


def crossref_year(item: dict) -> int | None:
    for key in ("published", "published-online", "published-print", "issued"):
        date_parts = (item.get(key) or {}).get("date-parts") or []
        if date_parts and date_parts[0]:
            try:
                return int(date_parts[0][0])
            except (TypeError, ValueError):
                pass
    return None


def doi_matches_source(doi: str, paper: dict) -> bool:
    if paper.get("source_system") == "nature":
        return doi.startswith("10.1038/")
    if paper.get("source_system") == "optica":
        if paper.get("source_name") == "JLT":
            return doi.startswith(("10.1109/jlt.", "10.1109/50."))
        return doi.startswith("10.1364/")
    return True


def select_crossref_match(paper: dict, items: list[dict]) -> dict | None:
    """Choose only a high-confidence title/year/source match."""
    expected_year = None
    try:
        expected_year = int(paper.get("year") or 0) or None
    except (TypeError, ValueError):
        pass

    best = None
    best_score = 0.0
    for item in items:
        doi = normalize_doi(item.get("DOI"))
        titles = item.get("title") or []
        title = titles[0] if titles else ""
        if not doi or not doi_matches_source(doi, paper):
            continue
        candidate_year = crossref_year(item)
        if expected_year and candidate_year and abs(expected_year - candidate_year) > 1:
            continue
        sequence, jaccard = title_similarity(paper.get("title") or "", title)
        if normalize_title(paper.get("title") or "") == normalize_title(title):
            score = 1.0
        elif sequence >= 0.94 and jaccard >= 0.78:
            score = (sequence + jaccard) / 2
        else:
            continue
        if score > best_score:
            best = item
            best_score = score
    return best


def parse_ieee_result(payload: dict, article_number: str) -> CitationResult | None:
    expected = str(article_number)
    for article in payload.get("articles") or []:
        if str(article.get("article_number") or "") != expected:
            continue
        raw_count = article.get("citing_paper_count")
        try:
            count = max(0, int(raw_count or 0))
        except (TypeError, ValueError):
            count = 0
        return CitationResult(
            count=count,
            source="ieee",
            doi=normalize_doi(article.get("doi")),
        )
    return None


def parse_ieee_results(
    payload: dict, article_numbers: set[str]
) -> dict[str, CitationResult]:
    results = {}
    for article in payload.get("articles") or []:
        key = str(article.get("article_number") or "")
        if key not in article_numbers:
            continue
        parsed = parse_ieee_result({"articles": [article]}, key)
        if parsed:
            results[key] = parsed
    return results


def parse_crossref_result(item: dict | None) -> CitationResult | None:
    if not item:
        return None
    try:
        count = max(0, int(item.get("is-referenced-by-count") or 0))
    except (TypeError, ValueError):
        count = 0
    return CitationResult(
        count=count,
        source="crossref",
        doi=normalize_doi(item.get("DOI")),
    )


class HttpClient:
    def __init__(self, delay: float):
        self.delay = max(0.0, delay)
        self.session = requests.Session()

    def get_json(self, url: str, params: dict | None = None, provider="provider"):
        for attempt in range(1, 5):
            response = self.session.get(url, params=params, timeout=(10, 35))
            status = response.status_code
            if status == 404:
                return None
            if status in (401, 403):
                raise ProviderQuotaError(f"{provider} access denied ({status})")
            if status == 429:
                if attempt == 4:
                    raise ProviderQuotaError(f"{provider} rate limit reached")
                retry_after = response.headers.get("Retry-After", "")
                try:
                    wait = min(60.0, max(1.0, float(retry_after)))
                except ValueError:
                    wait = min(60.0, 2**attempt)
                time.sleep(wait)
                continue
            if status >= 500:
                if attempt == 4:
                    raise CitationError(f"{provider} server error ({status})")
                time.sleep(2**attempt)
                continue
            if status != 200:
                raise CitationError(f"{provider} HTTP {status}")
            if self.delay:
                time.sleep(self.delay)
            try:
                return response.json()
            except ValueError as exc:
                raise CitationError(f"{provider} returned invalid JSON") from exc
        raise CitationError(f"{provider} request failed")


class IEEEClient(HttpClient):
    def __init__(self, api_key: str, delay: float):
        super().__init__(delay)
        self.api_key = api_key

    def fetch(self, article_number: str) -> CitationResult | None:
        return self.fetch_many([article_number]).get(str(article_number))

    def fetch_many(
        self, article_numbers: list[str]
    ) -> dict[str, CitationResult]:
        keys = [str(value) for value in article_numbers]
        payload = self.get_json(
            IEEE_API_URL,
            params={
                "apikey": self.api_key,
                "format": "json",
                # The exact article_number filter can report total_records=1
                # while omitting the articles array. Querying the public
                # document id as metadata text returns the record body; the
                # parser still requires an exact article_number match.
                "max_records": min(200, max(25, len(keys) * 4)),
                "querytext": " OR ".join(keys),
            },
            provider="IEEE",
        )
        return parse_ieee_results(payload or {}, set(keys))


class CrossrefClient(HttpClient):
    def __init__(self, delay: float):
        super().__init__(delay)
        contact = (os.getenv("CROSSREF_MAILTO") or "").strip()
        if "@" not in contact:
            contact = ""
        agent = "IEEE-Paper-Server/1.0"
        if contact:
            agent += f" (mailto:{contact})"
        self.session.headers.update({"User-Agent": agent})
        self.contact = contact

    def _params(self, values: dict | None = None) -> dict:
        params = dict(values or {})
        if self.contact:
            params["mailto"] = self.contact
        return params

    def fetch_doi(self, doi: str) -> CitationResult | None:
        payload = self.get_json(
            f"{CROSSREF_API_BASE}/{quote(doi, safe='')}",
            params=self._params(),
            provider="Crossref",
        )
        return parse_crossref_result((payload or {}).get("message"))

    def search_title(self, paper: dict) -> CitationResult | None:
        payload = self.get_json(
            CROSSREF_API_BASE,
            params=self._params(
                {
                    "query.bibliographic": paper.get("title") or "",
                    "rows": 5,
                    "select": (
                        "DOI,title,published,published-online,published-print,"
                        "issued,is-referenced-by-count"
                    ),
                }
            ),
            provider="Crossref",
        )
        items = ((payload or {}).get("message") or {}).get("items") or []
        return parse_crossref_result(select_crossref_match(paper, items))


def load_local_doi_map(
    target_keys: set[str], *, conflicts: dict[str, list[str]] | None = None
) -> dict[str, str]:
    """Recover only unambiguous DOI mappings, regardless of workbook order.

    A publisher URL can have more than one registered DOI. Keep those keys out
    of automatic recovery and optionally expose their candidates to the caller.
    """
    if not target_keys:
        return {}
    import pandas as pd

    candidates: dict[str, set[str]] = {}
    files = sorted(META_DIR.glob("*.xlsx"))
    wanted = {"Journal", "Conference", "URL", "DOI"}
    for index, path in enumerate(files, 1):
        try:
            frame = pd.read_excel(path, usecols=lambda column: column in wanted)
        except Exception:
            continue
        for row in frame.to_dict("records"):
            doi = normalize_doi(row.get("DOI"))
            if not doi:
                continue
            source = cell_text(row.get("Journal") or row.get("Conference"))
            if source == "JLT":
                key = jlt_doi_key(doi)
            else:
                key, _ = extract_key(cell_text(row.get("URL")))
            if key in target_keys:
                candidates.setdefault(key, set()).add(doi)
        if index % 15 == 0:
            print(f"  local DOI scan: {index}/{len(files)} files, {len(candidates)} keys")
    ambiguous = {key: sorted(dois) for key, dois in candidates.items() if len(dois) > 1}
    if conflicts is not None:
        conflicts.update(ambiguous)
    if ambiguous:
        print(f"  local DOI scan: skipped {len(ambiguous)} keys with multiple DOIs")
    return {key: next(iter(dois)) for key, dois in candidates.items() if len(dois) == 1}


def favorite_scope_sql(conn):
    """Acquisition scope is the union of personal favorites; no user data emitted."""
    cached = getattr(conn, '_account_favorite_scope', None)
    if isinstance(cached, str):
        return cached
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) n FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='account_favorites'")
        personal = bool(cur.fetchone()['n'])
    scope = ('(is_favorite=1 OR EXISTS (SELECT 1 FROM account_favorites af WHERE af.paper_id=papers.id))'
             if personal else 'is_favorite=1')
    conn._account_favorite_scope = scope
    return scope


def clear_nonfavorite_citations(conn) -> int:
    if favorite_scope_sql(conn) != 'is_favorite=1':
        # Bibliographic data is shared; removing a personal star must not erase it.
        return 0
    with conn.cursor() as cur:
        count = cur.execute(
            """
            UPDATE papers
            SET citation_count=NULL, citation_source=NULL, citation_updated_at=NULL
            WHERE is_favorite=0
              AND (
                citation_count IS NOT NULL
                OR citation_source IS NOT NULL
                OR citation_updated_at IS NOT NULL
              )
            """
        )
    conn.commit()
    return count


def fetch_targets(
    conn,
    *,
    refresh_days: int,
    force: bool,
    limit: int | None,
    source_system: str | None,
) -> list[dict]:
    where = [favorite_scope_sql(conn)]
    params = []
    if not force:
        threshold = datetime.now() - timedelta(days=max(0, refresh_days))
        where.append("(citation_updated_at IS NULL OR citation_updated_at < %s)")
        params.append(threshold)
    if source_system:
        where.append("source_system=%s")
        params.append(source_system)
    sql = f"""
        SELECT article_number, title, year, source_name, source_system, doi,
               citation_count, citation_updated_at
        FROM papers
        WHERE {' AND '.join(where)}
        ORDER BY
          CASE source_system WHEN 'nature' THEN 0 WHEN 'optica' THEN 1 ELSE 2 END,
          (year+0) DESC, id DESC
    """
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def store_result(conn, paper: dict, result: CitationResult):
    doi = result.doi or normalize_doi(paper.get("doi"))
    favorite_scope = favorite_scope_sql(conn)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE papers
            SET doi=COALESCE(%s, doi),
                citation_count=%s,
                citation_source=%s,
                citation_updated_at=UTC_TIMESTAMP()
            WHERE article_number=%s AND {favorite_scope}
            """,
            (doi, result.count, result.source, paper["article_number"]),
        )


def store_doi(conn, article_number: str, doi: str):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE papers SET doi=COALESCE(doi, %s) WHERE article_number=%s",
            (doi, article_number),
        )


def write_report(report: dict) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORT_DIR / f"citation_update_{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def citation_result_code(stats: dict) -> int:
    """Return a scheduler-visible failure for provider errors or quota stops."""
    return 1 if stats.get("quota_stopped") or int(stats.get("errors") or 0) else 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="즐겨찾기 논문의 인용수를 IEEE/Crossref에서 갱신"
    )
    parser.add_argument("--refresh-days", type=int, default=7)
    parser.add_argument("--force", action="store_true", help="최근 갱신 행도 다시 조회")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--source-system", choices=("ieee", "optica", "nature")
    )
    parser.add_argument("--delay", type=float, default=0.15)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--migrate-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main():
    args = build_parser().parse_args()
    conn = db_connect()
    try:
        added = ensure_schema(conn)
        print("Schema:", ", ".join(added) if added else "already current")
        if args.migrate_only:
            return

        cleared = 0 if args.dry_run else clear_nonfavorite_citations(conn)
        targets = fetch_targets(
            conn,
            refresh_days=args.refresh_days,
            force=args.force,
            limit=args.limit,
            source_system=args.source_system,
        )
        print(f"Targets: {len(targets):,}; cleared non-favorites: {cleared:,}")
        if not targets:
            return

        unresolved_keys = {
            str(row["article_number"])
            for row in targets
            if row["source_system"] != "ieee" and not normalize_doi(row.get("doi"))
        }
        local_doi_conflicts: dict[str, list[str]] = {}
        local_dois = load_local_doi_map(unresolved_keys, conflicts=local_doi_conflicts)
        for row in targets:
            key = str(row["article_number"])
            if normalize_doi(row.get("doi")):
                continue
            if key in local_doi_conflicts:
                # Neither key derivation nor a title search can choose which
                # of two registrations is the publisher's canonical DOI.
                continue
            derived = derive_doi(row)
            if derived:
                row["doi"] = derived
            elif key in local_dois:
                row["doi"] = local_dois[key]

        if args.dry_run:
            exact = sum(1 for row in targets if normalize_doi(row.get("doi")))
            print(f"Dry run: {exact:,}/{len(targets):,} targets have exact DOI mappings")
            return

        ieee_key = (os.getenv("IEEE_API_KEY") or "").strip()
        ieee_client = IEEEClient(ieee_key, args.delay) if ieee_key else None

        stats = {
            "targeted": len(targets),
            "updated": 0,
            "ieee": 0,
            "crossref": 0,
            "unresolved": 0,
            "errors": 0,
            "quota_stopped": False,
        }
        failures = []
        processed = 0

        def show_progress():
            print(
                f"  progress {processed:,}/{len(targets):,}: "
                f"updated={stats['updated']:,}, unresolved={stats['unresolved']:,}, "
                f"errors={stats['errors']:,}"
            )

        # Crossref lookups are independent and the public API supports
        # concurrent read requests. Each worker owns its own Session.
        non_ieee_targets = [
            paper for paper in targets if paper["source_system"] != "ieee"
        ]
        worker_state = threading.local()

        def crossref_lookup(paper):
            if str(paper["article_number"]) in local_doi_conflicts:
                return None
            client = getattr(worker_state, "crossref_client", None)
            if client is None:
                client = CrossrefClient(args.delay)
                worker_state.crossref_client = client
            doi = normalize_doi(paper.get("doi"))
            if doi:
                return client.fetch_doi(doi)
            return client.search_title(paper)

        if non_ieee_targets:
            with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
                futures = {
                    executor.submit(crossref_lookup, paper): paper
                    for paper in non_ieee_targets
                }
                for future in as_completed(futures):
                    paper = futures[future]
                    key = str(paper["article_number"])
                    doi = normalize_doi(paper.get("doi"))
                    processed += 1
                    try:
                        result = future.result()
                        if result:
                            store_result(conn, paper, result)
                            stats["updated"] += 1
                            stats[result.source] += 1
                        else:
                            if doi:
                                store_doi(conn, key, doi)
                            stats["unresolved"] += 1
                            failures.append(
                                {"article_number": key, "reason": (
                                    "ambiguous local DOI registrations"
                                    if key in local_doi_conflicts else "not found"
                                )}
                            )
                    except ProviderQuotaError as exc:
                        stats["quota_stopped"] = True
                        stats["errors"] += 1
                        failures.append({"article_number": key, "reason": str(exc)})
                    except (CitationError, requests.RequestException) as exc:
                        if doi:
                            store_doi(conn, key, doi)
                        stats["errors"] += 1
                        failures.append({"article_number": key, "reason": str(exc)})

                    if processed % max(1, args.batch_size) == 0:
                        conn.commit()
                        show_progress()

        # IEEE accepts Boolean metadata queries. Batching public document ids
        # reduces more than a thousand calls to a few dozen while exact-id
        # matching prevents false positives.
        ieee_targets = [
            paper for paper in targets if paper["source_system"] == "ieee"
        ]
        ieee_batch_size = min(40, max(1, args.batch_size))
        for start in range(0, len(ieee_targets), ieee_batch_size):
            batch = ieee_targets[start : start + ieee_batch_size]
            keys = [str(paper["article_number"]) for paper in batch]
            try:
                if not ieee_client:
                    raise CitationError("IEEE_API_KEY is not configured")
                batch_results = ieee_client.fetch_many(keys)
            except ProviderQuotaError as exc:
                stats["quota_stopped"] = True
                stats["errors"] += len(batch)
                failures.extend(
                    {"article_number": key, "reason": str(exc)} for key in keys
                )
                processed += len(batch)
                print(f"IEEE stopped safely after {processed}/{len(targets)}: {exc}")
                break
            except (CitationError, requests.RequestException) as exc:
                batch_results = {}
                stats["errors"] += len(batch)
                failures.extend(
                    {"article_number": key, "reason": str(exc)} for key in keys
                )

            for paper in batch:
                key = str(paper["article_number"])
                result = batch_results.get(key)
                if result:
                    store_result(conn, paper, result)
                    stats["updated"] += 1
                    stats[result.source] += 1
                elif not any(
                    failure["article_number"] == key for failure in failures[-len(batch) :]
                ):
                    stats["unresolved"] += 1
                    failures.append({"article_number": key, "reason": "not found"})
            processed += len(batch)
            conn.commit()
            show_progress()

        conn.commit()
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "stats": stats,
            "failures": failures,
        }
        report_path = write_report(report)
        print(json.dumps(stats, ensure_ascii=False))
        print(f"Report: {report_path}")
        result_code = citation_result_code(stats)
        if result_code:
            raise SystemExit(result_code)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
