"""Bounded public citation metadata from OpenAlex, with no database writes.

Related-work IDs are candidate hints, never evidence of a citation. References
and co-citation evidence are limited to the records in the returned snapshot.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html import unescape
import os
import re
import threading
import time
import unicodedata
from typing import Any
from urllib.parse import quote

import requests


OPENALEX_URL = "https://api.openalex.org/works"
MAX_REFERENCES = 80
MAX_CITERS = 60
MAX_RELATED = 10
MAX_CANDIDATES = 180
MAX_REQUESTS = 8
MAX_WORK_REFERENCES = 2000
MAX_ABSTRACT_LENGTH = 12000
FIELDS = ",".join(("id", "doi", "title", "publication_year", "authorships",
                   "primary_location", "cited_by_count", "referenced_works",
                   "related_works", "abstract_inverted_index"))
_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|(?:dx\.)?doi\.org/|doi: *)", re.I)
_DOI = re.compile(r"10\.\d{4,9}/\S+\Z", re.I)
_WORK_ID = re.compile(r"(?:https://openalex\.org/)?(W\d+)\Z", re.I)


class ProviderError(RuntimeError):
    """A safe public error; upstream response bodies/URLs are never exposed."""

    def __init__(self, message: str, status: int = 503):
        super().__init__(message)
        self.message = message
        self.status = status


def normalize_doi(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 300:
        raise ValueError("A valid DOI is required.")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError("A valid DOI is required.")
    doi = _DOI_PREFIX.sub("", value.strip()).lower()
    if len(doi) > 255 or not _DOI.fullmatch(doi) or any(char.isspace() for char in doi):
        raise ValueError("A valid DOI is required.")
    return doi


def _plain(value: Any, limit: int = 12000) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", "", unescape(value))).strip()[:limit]


def _work_id(value: Any) -> str | None:
    match = _WORK_ID.fullmatch(value) if isinstance(value, str) else None
    return match.group(1).upper() if match else None


def _ids(values: Any, limit: int) -> list[str]:
    if not isinstance(values, list):
        return []
    found: dict[str, None] = {}
    for value in values:
        identifier = _work_id(value)
        if identifier:
            found[identifier] = None
            if len(found) >= limit:
                break
    return list(found)


def _integer(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _optional_count(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
        return number if number >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _abstract(index: Any) -> str:
    """Reconstruct bounded, contiguous token positions without huge allocation."""
    if not isinstance(index, dict) or len(index) > MAX_ABSTRACT_LENGTH:
        return ""
    positioned: dict[int, str] = {}
    for token, positions in index.items():
        if not isinstance(token, str) or not isinstance(positions, list):
            return ""
        for position in positions[:MAX_ABSTRACT_LENGTH]:
            if isinstance(position, bool) or not isinstance(position, int) or position < 0:
                return ""
            if position >= MAX_ABSTRACT_LENGTH:
                continue
            if position in positioned and positioned[position] != token:
                return ""
            positioned[position] = token[:MAX_ABSTRACT_LENGTH]
    if not positioned or len(positioned) != max(positioned) + 1:
        return ""
    ordered: list[str] = []
    size = 0
    for position in range(len(positioned)):
        token = positioned[position]
        ordered.append(token)
        size += len(token) + 1
        if size >= MAX_ABSTRACT_LENGTH:
            break
    return _plain(" ".join(ordered))


def _normalize_work(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not (identifier := _work_id(raw.get("id"))):
        return None
    try:
        doi = normalize_doi(raw.get("doi"))
    except ValueError:
        doi = ""
    location = raw.get("primary_location")
    source = location.get("source") if isinstance(location, dict) else None
    authors: list[str] = []
    authorships = raw.get("authorships")
    for item in authorships[:100] if isinstance(authorships, list) else []:
        author = item.get("author") if isinstance(item, dict) else None
        if isinstance(author, dict) and (name := _plain(author.get("display_name"), 200)):
            authors.append(name)
    references = _ids(raw.get("referenced_works"), MAX_WORK_REFERENCES)
    raw_references = raw.get("referenced_works")
    year = _integer(raw.get("publication_year"))
    return {
        "id": identifier,
        "doi": doi,
        "title": _plain(raw.get("title") or raw.get("display_name"), 2000),
        "authors": ", ".join(authors),
        "year": year if 1000 <= year <= 2200 else None,
        "source_name": _plain(source.get("display_name"), 300) if isinstance(source, dict) else "",
        "source_system": "OpenAlex",
        "citation_count": _optional_count(raw.get("cited_by_count")),
        "reference_ids": references,
        "related_ids": _ids(raw.get("related_works"), MAX_RELATED),
        "abstract": _abstract(raw.get("abstract_inverted_index")),
        "references_truncated": isinstance(raw_references, list) and len(raw_references) > MAX_WORK_REFERENCES,
    }


class OpenAlexProvider:
    def __init__(self, session=None, api_key: str | None = None):
        self.session = session
        self._sessions = threading.local()
        self.api_key = os.environ.get("OPENALEX_API_KEY", "") if api_key is None else api_key

    def lookup_dois(self, dois):
        """A single bounded exact-identifier batch; never infer a citation here."""
        values = list(dict.fromkeys(normalize_doi(doi) for doi in dois))[:80]
        if not values:
            return []
        state = {"request_count": 0, "deadline": time.monotonic()+20}
        try:
            payload = self._request('', {'filter': 'doi:'+'|'.join(values), 'per_page':100, 'select':FIELDS}, state)
        finally:
            self._sessions.lookup_request_count = state['request_count']
        return [work for raw in (payload.get('results') or [])[:100]
                if (work := _normalize_work(raw)) and work['doi'] in values]

    def _request(self, path: str, params: dict, state: dict) -> dict:
        # Worker threads reuse their own connection pools; a shared provider
        # never concurrently mutates a requests.Session cookie/header state.
        client = self.session
        if client is None:
            client = getattr(self._sessions, "client", None)
            if client is None:
                client = self._sessions.client = requests.Session()
        headers = {"Accept": "application/json", "User-Agent": "Paper-Archive/1.0 (citation-map)"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        for attempt in range(2):
            if state["request_count"] >= MAX_REQUESTS or time.monotonic() >= state["deadline"]:
                raise ProviderError("인용 데이터 조회 한도에 도달했습니다. 잠시 후 다시 시도해 주세요.")
            state["request_count"] += 1
            try:
                response = client.get(OPENALEX_URL + path, params=params, headers=headers,
                                            timeout=(3.05, 12), allow_redirects=False)
            except (requests.RequestException, ValueError):
                if attempt == 0:
                    time.sleep(0.25)
                    continue
                raise ProviderError("OpenAlex에 일시적으로 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.") from None
            status = response.status_code
            if status == 404:
                raise ProviderError("OpenAlex에서 이 DOI의 논문을 찾지 못했습니다.", 404)
            if status in (401, 403):
                raise ProviderError("OpenAlex 접근이 제한되었습니다. 서버 API 설정을 확인해 주세요.")
            if status == 429 or 500 <= status < 600:
                if attempt == 0:
                    retry_after = response.headers.get("Retry-After", "")
                    try:
                        delay = max(0.25, float(retry_after))
                    except (TypeError, ValueError):
                        delay = 0.25
                    if delay <= 2:
                        time.sleep(delay)
                        continue
                if status == 429:
                    raise ProviderError("OpenAlex 사용량 한도에 도달했습니다. 잠시 후 다시 시도해 주세요.", 429)
                raise ProviderError("OpenAlex에 일시적으로 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.")
            if status != 200:
                raise ProviderError("OpenAlex에서 인용 데이터 조회를 완료하지 못했습니다.")
            try:
                data = response.json()
            except (ValueError, requests.RequestException):
                raise ProviderError("OpenAlex 응답을 처리할 수 없습니다. 잠시 후 다시 시도해 주세요.") from None
            if not isinstance(data, dict):
                raise ProviderError("OpenAlex 응답을 처리할 수 없습니다. 잠시 후 다시 시도해 주세요.")
            return data
        raise ProviderError("OpenAlex에 일시적으로 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.")

    def fetch(self, doi: str) -> dict[str, Any]:
        doi = normalize_doi(doi)
        state = {"request_count": 0, "deadline": time.monotonic() + 50}
        raw_seed = self._request("/https://doi.org/" + quote(doi, safe="/"), {"select": FIELDS}, state)
        seed = _normalize_work(raw_seed)
        if seed is None or seed["doi"] != doi:
            raise ProviderError("OpenAlex 응답에서 요청한 DOI를 확인하지 못했습니다.", 502)
        works = {seed["id"]: seed}
        warnings: list[str] = []

        def listing(params: dict, purpose: str) -> tuple[list[dict], int | None]:
            try:
                payload = self._request("", {"select": FIELDS, **params}, state)
                if not isinstance(payload.get("results"), list):
                    raise ProviderError("OpenAlex에서 올바른 논문 목록을 받지 못했습니다.", 502)
                meta = payload.get("meta")
                total = _optional_count(meta.get("count")) if isinstance(meta, dict) else None
                return payload["results"][:100], total
            except ProviderError as error:
                warnings.append(f"{purpose}: {error.message}")
                return [], None

        def add_records(records: list[dict], limit: int = MAX_CANDIDATES) -> list[str]:
            identifiers: list[str] = []
            for raw in records:
                work = _normalize_work(raw)
                if work is None or not work["title"]:
                    continue
                if len(works) >= limit and work["id"] not in works:
                    continue
                works[work["id"]] = work
                identifiers.append(work["id"])
            return identifiers

        reference_ids = seed["reference_ids"][:MAX_REFERENCES]
        neighbor_ids = list(dict.fromkeys(reference_ids + seed["related_ids"]))
        if neighbor_ids:
            records, _ = listing({"filter": "openalex:" + "|".join(neighbor_ids),
                                  "per_page": min(100, len(neighbor_ids))}, "참고문헌")
            add_records(records)
        records, citing_total = listing({"filter": "cites:" + seed["id"],
                                         "per_page": MAX_CITERS,
                                         "sort": "cited_by_count:desc"}, "인용 논문")
        citer_ids = add_records(records[:MAX_CITERS])

        # Papers sharing uncommon seed references can add useful siblings beyond
        # the seed's immediate incoming/outgoing neighborhood. No inferred edge
        # is created here: every candidate still carries its observed references.
        available_refs = [works[identifier] for identifier in reference_ids if identifier in works]
        available_refs.sort(key=lambda work: (work["citation_count"] if work["citation_count"] is not None else float("inf"),
                                               -(work["year"] or 0), work["id"]))
        sibling_refs = [work["id"] for work in available_refs[:8]]
        sibling_total: int | None = None
        sibling_ids: list[str] = []
        if sibling_refs and len(works) < MAX_CANDIDATES and not warnings:
            records, sibling_total = listing({"filter": "cites:" + "|".join(sibling_refs),
                                                "per_page": min(60, MAX_CANDIDATES - len(works)),
                                                "sort": "cited_by_count:desc"}, "공통 참고문헌 후보")
            sibling_ids = add_records(records)

        raw_refs = raw_seed.get("referenced_works")
        reference_total = len(raw_refs) if isinstance(raw_refs, list) else 0
        reference_fetched = sum(identifier in works for identifier in reference_ids)
        citing_total = seed["citation_count"] if citing_total is None else citing_total
        reference_truncated = reference_fetched < reference_total
        citing_truncated = citing_total is None or len(set(citer_ids)) < citing_total
        return {
            "seed_id": seed["id"],
            "works": list(works.values()),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "meta": {
                "provider": "OpenAlex", "provider_url": "https://openalex.org/", "license": "CC0",
                "reference_total": reference_total, "citing_total": citing_total,
                "reference_fetched": reference_fetched, "citing_fetched": len(set(citer_ids)),
                "reference_limit": MAX_REFERENCES, "citing_limit": MAX_CITERS,
                "reference_truncated": reference_truncated, "citing_truncated": citing_truncated,
                "candidate_limit": MAX_CANDIDATES, "candidate_count": len(works),
                "sibling_query_reference_count": len(sibling_refs), "sibling_total": sibling_total,
                "sibling_fetched": len(set(sibling_ids)), "request_count": state["request_count"],
                "coverage_truncated": bool(warnings or reference_truncated or citing_truncated
                                           or sibling_total is not None and sibling_total > len(set(sibling_ids))
                                           or any(work["references_truncated"] for work in works.values())),
                "coverage_note": "OpenAlex에서 조회한 주변 논문 범위의 지도입니다. 동시 인용은 조회된 논문에서만 계산하며, 연결이 없다고 관련성이 없는 것은 아닙니다.",
                "warnings": warnings,
            },
        }
