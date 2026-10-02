#!/usr/bin/env python3
"""Read-only, resumable validation of Nature DOI candidates against Crossref.

Repeated DOI filters use Crossref's documented OR semantics. Each response is
cached with the requested identifiers and each candidate is validated separately;
a successful HTTP batch is not evidence that all of its candidates were found.
No database connection or write is performed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
from html import unescape
import json
from pathlib import Path
import re
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.fetch_ieee_crossref import CrossrefClient

CONTAINERS = {
    "NPHOTON": "Nature Photonics",
    "NELECTRON": "Nature Electronics",
    "NCOMMS": "Nature Communications",
}


def normalized_title(value: str) -> str:
    text = unicodedata.normalize("NFKC", unescape(value or ""))
    text = re.sub(r"<[^>]+>", "", text).casefold()
    return "".join(character for character in text if character.isalnum())


def validate_candidate(candidate: dict, records: list[dict]) -> dict:
    doi = str(candidate["proposed_doi"]).strip().lower()
    result = {**candidate, "status": "not_found", "evidence": []}
    same_doi = [record for record in records if str(record.get("DOI", "")).lower() == doi]
    if len(same_doi) != 1:
        result["status"] = "not_found" if not same_doi else "multiple_records"
        return result
    record = same_doi[0]
    result["evidence"] = [record]
    expected_key = str(candidate["article_number"]).lower()
    if doi != "10.1038/" + expected_key:
        result["status"] = "key_mismatch"
    elif CONTAINERS.get(candidate["source_name"]) not in record.get("container-title", []):
        result["status"] = "source_mismatch"
    elif not normalized_title(candidate["title"]) or not any(
        normalized_title(title) == normalized_title(candidate["title"])
        for title in record.get("title", [])
    ):
        result["status"] = "title_mismatch"
    else:
        result["status"] = "verified"
    return result


def write_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 100:
        parser.error("batch size must be between 1 and 100")
    candidates = json.loads(args.candidates.read_text(encoding="utf-8"))
    if len({candidate["article_number"] for candidate in candidates}) != len(candidates):
        raise ValueError("Duplicate article keys in candidate file")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = args.output_dir / "crossref_batches"
    cache_dir.mkdir(exist_ok=True)
    client = CrossrefClient()
    results = []
    batches = []
    for start in range(0, len(candidates), args.batch_size):
        batch = candidates[start:start + args.batch_size]
        dois = [candidate["proposed_doi"].lower() for candidate in batch]
        digest = sha256("\n".join(dois).encode()).hexdigest()[:16]
        cache_path = cache_dir / f"{start:05d}_{digest}.json"
        if cache_path.exists():
            evidence = json.loads(cache_path.read_text(encoding="utf-8"))
            if evidence["requested_dois"] != dois:
                raise ValueError(f"Cache key mismatch: {cache_path}")
        else:
            params = {
                "filter": ",".join("doi:" + doi for doi in dois),
                "rows": 1000,
                "select": "DOI,title,container-title,resource,URL,published",
            }
            message = client.get_message("/works", params)
            evidence = {
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "endpoint": "https://api.crossref.org/works",
                "requested_dois": dois,
                "query": params,
                "message": message,
            }
            write_json(cache_path, evidence)
        message = evidence["message"]
        records = message.get("items", [])
        if message.get("total-results", 0) > len(records):
            raise ValueError(f"Incomplete batch response: {cache_path}")
        unexpected = {record["DOI"].lower() for record in records} - set(dois)
        if unexpected:
            raise ValueError(f"Crossref filter returned unrequested DOI(s): {unexpected}")
        for candidate in batch:
            result = validate_candidate(candidate, records)
            result["evidence_file"] = str(cache_path.relative_to(args.output_dir))
            results.append(result)
        batches.append({"file": cache_path.name, "requested": len(dois), "returned": len(records)})
        print(f"Validated {len(results)}/{len(candidates)}: {dict(Counter(r['status'] for r in results))}", flush=True)
    verified = [result for result in results if result["status"] == "verified"]
    unresolved = [result for result in results if result["status"] != "verified"]
    summary = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "candidate_count": len(candidates),
        "status_counts": dict(Counter(result["status"] for result in results)),
        "verified_by_source": dict(Counter(result["source_name"] for result in verified)),
        "candidate_sha256": sha256(args.candidates.read_bytes()).hexdigest(),
        "batch_count": len(batches),
        "batches": batches,
        "normalization": "HTML entities/tags and Unicode compatibility normalized; punctuation/spacing ignored; all Unicode letters/numbers retained",
        "read_only": True,
    }
    write_json(args.output_dir / "nature_verified_candidates.json", verified)
    write_json(args.output_dir / "nature_unresolved_candidates.json", unresolved)
    write_json(args.output_dir / "nature_validation_summary.json", summary)
    print(json.dumps({key: value for key, value in summary.items() if key != "batches"}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
