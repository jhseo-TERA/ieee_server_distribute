"""Conservative, non-destructive conference-to-journal implementation families.

The module never moves or deletes a SerDes measurement.  It records auditable
paper-pair candidates and assigns only strict reciprocal-best matches (or an
explicit human approval) to a family that API queries can join by ``paper_id``.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Iterable

from serdes_metrics import (
    extract_performance,
    normalize_title,
    plain_text,
    taxonomy,
    title_similarity,
)


IMPLEMENTATION_FAMILY_VERSION = "serdes-implementation-family-1.1"

# Only publication paths with a well-established conference-to-journal pattern
# are eligible.  Adding a venue is a classifier-version change, not a fuzzy DB
# search option.
CONFERENCE_JOURNAL_VENUES = {
    "ISSCC": frozenset({"JSSC"}),
    "CICC": frozenset({"JSSC", "TCAS-I", "TCAS-II", "OJSSC"}),
    "VLSI-Circuits": frozenset({"JSSC"}),
    "VLSI-Tech": frozenset({"JSSC"}),
    "ASSCC": frozenset({"JSSC", "OJSSC"}),
    "ISCAS": frozenset({"TCAS-I", "TCAS-II", "JSSC", "OJSSC"}),
    "RFIC": frozenset({"JSSC", "MWTL", "MWCL", "OJSSC"}),
    "ICTA": frozenset({"TCAS-I", "TCAS-II"}),
}

TECHNICAL_TITLE_STOPWORDS = frozenset({
    "and", "the", "for", "with", "from", "using", "this", "that",
    "into", "based", "their", "over", "under", "through", "high",
    "low", "cmos", "finfet", "soi", "nm", "um", "gb", "gbps", "ghz",
    "mhz", "bit", "bits", "data", "design", "link", "circuit", "system",
    "of", "in", "on", "to", "by", "at", "as", "is", "its", "via",
})

FAMILY_ELIGIBLE_DECISIONS = frozenset({"auto_grouped", "approved"})
MANUAL_DECISIONS = frozenset({"manual_review", "approved", "rejected"})

# ``auto_grouped`` is deliberately absent: classifier-owned automatic matches
# must be allowed to demote when the evidence or reciprocal-best set changes.
# Review states remain sticky even when they pre-date explicit source tagging.
STICKY_DECISIONS = MANUAL_DECISIONS


def candidate_key(conference_paper_id: int, journal_paper_id: int) -> str:
    payload = f"{int(conference_paper_id)}:{int(journal_paper_id)}"
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def family_key(journal_paper_id: int) -> str:
    return f"ieee-journal-family:{int(journal_paper_id)}"


def resolve_decision_status(
    existing_status: str | None,
    proposed_status: str,
    existing_source: str | None = None,
) -> str:
    """Keep every prior review/decision stable across classifier rebuilds."""
    existing = str(existing_status or "").strip().lower()
    source = str(existing_source or "").strip().lower()
    if source == "manual" or existing in STICKY_DECISIONS:
        return existing
    return proposed_status


def decision_is_family_eligible(status: str | None) -> bool:
    return str(status or "").strip().lower() in FAMILY_ELIGIBLE_DECISIONS


def decision_is_sticky(
    existing_status: str | None,
    existing_source: str | None = None,
) -> bool:
    existing = str(existing_status or "").strip().lower()
    source = str(existing_source or "").strip().lower()
    return source == "manual" or existing in STICKY_DECISIONS


def select_family_candidates(
    candidates: Iterable[dict],
) -> tuple[list[dict], list[dict]]:
    """Choose at most one journal family for each conference paper.

    Manual approvals take precedence over classifier decisions; remaining ties
    are resolved by score and stable database id.  Multiple conference papers
    may still belong to the same canonical journal family.
    """
    eligible = [
        dict(row) for row in candidates
        if decision_is_family_eligible(row.get("decision_status"))
    ]
    eligible.sort(key=lambda row: (
        0 if str(row.get("decision_status")) == "approved" else 1,
        -float(row.get("match_score") or 0),
        int(row.get("id") or 0),
    ))
    selected: list[dict] = []
    conflicts: list[dict] = []
    used_conference_papers: set[int] = set()
    for row in eligible:
        conference_id = int(row["conference_paper_id"])
        if conference_id in used_conference_papers:
            conflicts.append(row)
            continue
        used_conference_papers.add(conference_id)
        selected.append(row)
    return selected, conflicts


def validate_family_snapshot(
    families: Iterable[dict],
    members: Iterable[dict],
    candidates: Iterable[dict],
) -> list[str]:
    """Validate the materialized family projection without a database.

    Eligible candidates may intentionally have no family when they conflict
    with a higher-priority match.  Once ``family_id`` is set, however, both
    papers and their current implementation ids must agree with the members.
    """
    family_rows = {int(row["id"]): dict(row) for row in families}
    member_rows = [dict(row) for row in members]
    candidate_rows = [dict(row) for row in candidates]
    members_by_family: dict[int, list[dict]] = defaultdict(list)
    member_by_paper: dict[int, dict] = {}
    violations: list[str] = []

    for member in member_rows:
        family_id = int(member["family_id"])
        paper_id = int(member["paper_id"])
        members_by_family[family_id].append(member)
        if family_id not in family_rows:
            violations.append(
                f"member paper {paper_id} references missing family {family_id}"
            )
        prior = member_by_paper.get(paper_id)
        if prior is not None:
            violations.append(
                f"paper {paper_id} belongs to families "
                f"{int(prior['family_id'])} and {family_id}"
            )
        else:
            member_by_paper[paper_id] = member

    grouped_candidates_by_family: dict[int, list[dict]] = defaultdict(list)
    candidate_by_id = {
        int(row["id"]): row for row in candidate_rows if row.get("id") is not None
    }
    for candidate in candidate_rows:
        candidate_id = int(candidate["id"])
        family_value = candidate.get("family_id")
        eligible = decision_is_family_eligible(candidate.get("decision_status"))
        if family_value is None:
            continue
        family_id = int(family_value)
        if not eligible:
            violations.append(
                f"ineligible candidate {candidate_id} retains family {family_id}"
            )
            continue
        grouped_candidates_by_family[family_id].append(candidate)
        family = family_rows.get(family_id)
        if family is None:
            violations.append(
                f"candidate {candidate_id} references missing family {family_id}"
            )
            continue
        if str(family.get("review_status")) != "active":
            violations.append(
                f"candidate {candidate_id} references inactive family {family_id}"
            )

        conference_id = int(candidate["conference_paper_id"])
        journal_id = int(candidate["journal_paper_id"])
        conference_member = member_by_paper.get(conference_id)
        journal_member = member_by_paper.get(journal_id)
        if (
            conference_member is None
            or int(conference_member["family_id"]) != family_id
            or bool(conference_member.get("is_canonical"))
            or int(conference_member.get("match_candidate_id") or 0) != candidate_id
        ):
            violations.append(
                f"candidate {candidate_id} conference member is inconsistent"
            )
        if (
            journal_member is None
            or int(journal_member["family_id"]) != family_id
            or not bool(journal_member.get("is_canonical"))
            or journal_id != int(family["canonical_paper_id"])
        ):
            violations.append(
                f"candidate {candidate_id} journal member is inconsistent"
            )

        for prefix, member in (
            ("conference", conference_member), ("journal", journal_member),
        ):
            expected = candidate.get(f"{prefix}_implementation_id")
            if expected is None or member is None:
                continue
            actual = member.get("implementation_id")
            if actual is None or int(actual) != int(expected):
                violations.append(
                    f"candidate {candidate_id} {prefix} implementation is stale"
                )

    for family_id, family in family_rows.items():
        active = str(family.get("review_status")) == "active"
        family_members = members_by_family.get(family_id, [])
        canonical_members = [
            member for member in family_members if bool(member.get("is_canonical"))
        ]
        if not active:
            if family_members:
                violations.append(f"inactive family {family_id} retains members")
            continue
        if len(canonical_members) != 1:
            violations.append(
                f"active family {family_id} has {len(canonical_members)} canonical members"
            )
        elif int(canonical_members[0]["paper_id"]) != int(
            family["canonical_paper_id"]
        ):
            violations.append(
                f"active family {family_id} canonical paper is inconsistent"
            )
        if not grouped_candidates_by_family.get(family_id):
            violations.append(f"active family {family_id} has no grouped candidate")

    for member in member_rows:
        if bool(member.get("is_canonical")):
            continue
        candidate_id = member.get("match_candidate_id")
        candidate = candidate_by_id.get(int(candidate_id or 0))
        if candidate is None or not decision_is_family_eligible(
            candidate.get("decision_status")
        ) or candidate.get("family_id") is None or int(
            candidate["family_id"]
        ) != int(member["family_id"]):
            violations.append(
                f"conference member paper {int(member['paper_id'])} has no eligible candidate"
            )

    return violations


def normalize_family_title(value: str | None, venue: str | None = None) -> str:
    """Normalize publisher markup and known conference session prefixes."""
    text = plain_text(value)
    if venue in CONFERENCE_JOURNAL_VENUES:
        # IEEE conference metadata sometimes stores "6.1 A ..." or "8 An ..."
        # as part of the paper title.  Requiring the following article avoids
        # deleting a genuine leading performance number such as "112-Gb/s".
        text = re.sub(
            r"^\s*\d+(?:\.\d+)?\s+(?=(?:a|an)\b)", "", text,
            flags=re.IGNORECASE,
        )
    # IEEE sources vary between ``112Gb/s``/``112-Gb/s`` and ``7nm``/``7-nm``.
    # Normalize only alphanumeric boundaries; decimal values stay untouched.
    text = re.sub(r"(?<=\d)(?=[A-Za-zµμ])", " ", text)
    text = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", text)
    return normalize_title(text)


def normalize_author_names(value: str | None) -> tuple[str, ...]:
    raw = str(value or "").replace("\r\n", ";").replace("\n", ";")
    text = plain_text(raw)
    if not text:
        return ()
    if ";" in text:
        chunks = text.split(";")
    else:
        chunks = re.sub(r",?\s+and\s+", ",", text, flags=re.I).split(",")
    names = []
    for chunk in chunks:
        normalized = " ".join(re.findall(r"[a-z]+", chunk.lower()))
        if normalized:
            names.append(normalized)
    return tuple(names)


def author_surnames(value: str | None) -> tuple[str, frozenset[str]]:
    names = normalize_author_names(value)
    surnames = frozenset(name.split()[-1] for name in names if name.split())
    first = names[0].split()[-1] if names else ""
    return first, surnames


def _technical_title_tokens(normalized: str) -> frozenset[str]:
    return frozenset(
        token for token in normalized.split()
        if len(token) > 2 and token not in TECHNICAL_TITLE_STOPWORDS
    )


def _rate_intervals(metrics: dict) -> tuple[tuple[float, float], ...]:
    minimum = metrics.get("reported_rate_min_gbps")
    maximum = metrics.get("reported_rate_max_gbps")
    if minimum is not None and maximum is not None:
        return ((float(minimum), float(maximum)),)
    intervals = []
    for item in metrics.get("candidates", {}).get("rates", []):
        if item.get("value") is None or str(item.get("kind", "")).startswith("symbol"):
            continue
        value = float(item["value"])
        intervals.append((value, value))
    return tuple(intervals)


def _rate_comparison(left: dict, right: dict) -> str:
    left_intervals = _rate_intervals(left)
    right_intervals = _rate_intervals(right)
    if not left_intervals or not right_intervals:
        return "missing"
    for left_min, left_max in left_intervals:
        for right_min, right_max in right_intervals:
            # Ranges that overlap after a 2% presentation tolerance agree.
            if max(left_min, right_min) <= min(left_max, right_max) * 1.02:
                return "match"
            endpoint_delta = min(
                abs(left_min - right_min) / max(left_min, right_min),
                abs(left_max - right_max) / max(left_max, right_max),
            )
            if endpoint_delta <= 0.05:
                return "match"
    return "conflict"


def _numeric_comparison(
    left: float | None,
    right: float | None,
    relative_tolerance: float,
    absolute_tolerance: float = 0.0,
) -> str:
    if left is None or right is None:
        return "missing"
    left_value, right_value = float(left), float(right)
    delta = abs(left_value - right_value)
    if delta <= absolute_tolerance:
        return "match"
    if delta / max(abs(left_value), abs(right_value), 1e-12) <= relative_tolerance:
        return "match"
    return "conflict"


def paper_match_features(paper: dict) -> dict:
    result = dict(paper)
    venue = str(result.get("source_name") or "")
    normalized = normalize_family_title(result.get("title"), venue)
    first_author, surnames = author_surnames(result.get("authors"))
    result.update({
        "normalized_family_title": normalized,
        "technical_title_tokens": _technical_title_tokens(normalized),
        "first_author_surname": first_author,
        "author_surnames": surnames,
        "performance_features": extract_performance(result.get("title")),
        "taxonomy_features": taxonomy(result.get("title")),
    })
    return result


def compare_technical_fingerprint(left: dict, right: dict) -> dict:
    matches: list[str] = []
    conflicts: list[str] = []
    left_metrics = left["performance_features"]
    right_metrics = right["performance_features"]

    comparisons = {
        "process": _numeric_comparison(
            left_metrics.get("process_nm"), right_metrics.get("process_nm"),
            0.02, 0.5,
        ),
        "rate": _rate_comparison(left_metrics, right_metrics),
        "energy": _numeric_comparison(
            left_metrics.get("energy_pj_bit"), right_metrics.get("energy_pj_bit"),
            0.10,
        ),
        "power": _numeric_comparison(
            left_metrics.get("power_mw"), right_metrics.get("power_mw"), 0.10,
        ),
    }
    for name, status in comparisons.items():
        if status == "match":
            matches.append(name)
        elif status == "conflict":
            conflicts.append(name)

    left_signals = {
        value for value in left["taxonomy_features"].get("signals", [])
        if value != "Other"
    }
    right_signals = {
        value for value in right["taxonomy_features"].get("signals", [])
        if value != "Other"
    }
    if left_signals and right_signals:
        (matches if left_signals & right_signals else conflicts).append("signal")

    left_blocks = set(left["taxonomy_features"].get("blocks", []))
    right_blocks = set(right["taxonomy_features"].get("blocks", []))
    left_tx_only = "TX" in left_blocks and "RX" not in left_blocks
    left_rx_only = "RX" in left_blocks and "TX" not in left_blocks
    right_tx_only = "TX" in right_blocks and "RX" not in right_blocks
    right_rx_only = "RX" in right_blocks and "TX" not in right_blocks
    if (left_tx_only and right_rx_only) or (left_rx_only and right_tx_only):
        conflicts.append("block")
    elif left_blocks & right_blocks:
        matches.append("block")

    left_medium = str(left.get("link_medium") or "unspecified").lower()
    right_medium = str(right.get("link_medium") or "unspecified").lower()
    if left_medium != "unspecified" and right_medium != "unspecified":
        (matches if left_medium == right_medium else conflicts).append("medium")

    return {
        "matches": sorted(set(matches)),
        "conflicts": sorted(set(conflicts)),
        "comparisons": comparisons,
    }


def _candidate_for_pair(conference: dict, journal: dict) -> dict | None:
    try:
        year_gap = int(journal.get("year")) - int(conference.get("year"))
    except (TypeError, ValueError):
        return None
    if not 0 <= year_gap <= 3:
        return None

    shared_authors = conference["author_surnames"] & journal["author_surnames"]
    shared_author_count = len(shared_authors)
    author_union = conference["author_surnames"] | journal["author_surnames"]
    author_jaccard = shared_author_count / max(1, len(author_union))
    first_author_match = bool(
        conference["first_author_surname"]
        and conference["first_author_surname"] == journal["first_author_surname"]
    )

    left_tokens = conference["technical_title_tokens"]
    right_tokens = journal["technical_title_tokens"]
    token_union = left_tokens | right_tokens
    token_jaccard = len(left_tokens & right_tokens) / max(1, len(token_union))
    if shared_author_count < 2 or token_jaccard < 0.25:
        return None

    left_title = conference["normalized_family_title"]
    right_title = journal["normalized_family_title"]
    similarity = title_similarity(left_title, right_title)
    exact_title = bool(left_title and left_title == right_title)
    fingerprint = compare_technical_fingerprint(conference, journal)
    technical_matches = fingerprint["matches"]
    technical_conflicts = fingerprint["conflicts"]
    author_ok = first_author_match or (
        shared_author_count >= 4 and author_jaccard >= 0.60
    )

    raw_strict = bool(
        not technical_conflicts
        and author_ok
        and (
            (exact_title and year_gap <= 3)
            or (
                year_gap <= 2
                and similarity >= 0.82
                and token_jaccard >= 0.60
                and (technical_matches or similarity >= 0.93)
            )
        )
    )
    raw_review = bool(
        not raw_strict
        and author_ok
        and similarity >= 0.65
        and token_jaccard >= 0.40
    )
    if not raw_strict and not raw_review:
        return None

    score = (
        0.55 * similarity
        + 0.15 * min(1.0, shared_author_count / 3)
        + 0.12 * author_jaccard
        + 0.12 * token_jaccard
        + 0.06 * int(first_author_match)
        - 0.20 * int(bool(technical_conflicts))
    )
    score = max(0.0, min(1.0, score))
    reasons = ["allowed_venue_pair", "conference_not_after_journal"]
    if exact_title:
        reasons.append("normalized_title_exact")
    if first_author_match:
        reasons.append("first_author_match")
    if technical_matches:
        reasons.append("technical_fingerprint_match")
    if technical_conflicts:
        reasons.append("technical_fingerprint_conflict")

    return {
        "candidate_key": candidate_key(conference["id"], journal["id"]),
        "conference_paper_id": int(conference["id"]),
        "journal_paper_id": int(journal["id"]),
        "conference_implementation_id": conference.get("implementation_id"),
        "journal_implementation_id": journal.get("implementation_id"),
        "conference_article_number": conference.get("article_number"),
        "journal_article_number": journal.get("article_number"),
        "conference_venue": conference.get("source_name"),
        "journal_venue": journal.get("source_name"),
        "conference_title": conference.get("title"),
        "journal_title": journal.get("title"),
        "year_gap": year_gap,
        "title_similarity": float(similarity),
        "title_token_jaccard": float(token_jaccard),
        "shared_author_count": shared_author_count,
        "author_jaccard": float(author_jaccard),
        "first_author_match": first_author_match,
        "technical_matches": technical_matches,
        "technical_conflicts": technical_conflicts,
        "technical_comparisons": fingerprint["comparisons"],
        "match_score": score,
        "raw_strict": raw_strict,
        "exact_title": exact_title,
        "reason_codes": reasons,
    }


def build_match_candidates(
    papers: Iterable[dict], reciprocal_margin: float = 0.04,
) -> list[dict]:
    """Return strict reciprocal-best pairs plus a bounded review queue."""
    prepared = [paper_match_features(dict(row)) for row in papers]
    by_venue: dict[str, list[dict]] = defaultdict(list)
    for row in prepared:
        by_venue[str(row.get("source_name") or "")].append(row)

    candidates = []
    for conference in prepared:
        journals = CONFERENCE_JOURNAL_VENUES.get(
            str(conference.get("source_name") or ""), (),
        )
        for venue in journals:
            for journal in by_venue.get(venue, []):
                candidate = _candidate_for_pair(conference, journal)
                if candidate:
                    candidates.append(candidate)

    by_conference: dict[int, list[dict]] = defaultdict(list)
    by_journal: dict[int, list[dict]] = defaultdict(list)
    for candidate in candidates:
        by_conference[candidate["conference_paper_id"]].append(candidate)
        by_journal[candidate["journal_paper_id"]].append(candidate)
    for group in (*by_conference.values(), *by_journal.values()):
        group.sort(
            key=lambda item: (
                -item["match_score"], item["conference_paper_id"],
                item["journal_paper_id"],
            )
        )

    for candidate in candidates:
        conference_group = by_conference[candidate["conference_paper_id"]]
        journal_group = by_journal[candidate["journal_paper_id"]]
        reciprocal = (
            conference_group[0] is candidate and journal_group[0] is candidate
        )
        conference_margin = candidate["match_score"] - (
            conference_group[1]["match_score"] if len(conference_group) > 1 else 0.0
        )
        journal_margin = candidate["match_score"] - (
            journal_group[1]["match_score"] if len(journal_group) > 1 else 0.0
        )
        # Even an exact title must not silently choose between two tied journal
        # records.  A singleton side has no rival; otherwise both margins must
        # clear the threshold.
        unambiguous = (
            (len(conference_group) == 1 or conference_margin >= reciprocal_margin)
            and (len(journal_group) == 1 or journal_margin >= reciprocal_margin)
        )
        strict = bool(candidate["raw_strict"] and reciprocal and unambiguous)
        candidate.update({
            "reciprocal_best": reciprocal,
            "conference_margin": max(0.0, conference_margin),
            "journal_margin": max(0.0, journal_margin),
            "candidate_tier": "strict" if strict else "manual_review",
            "proposed_decision_status": (
                "auto_grouped" if strict else "manual_review"
            ),
        })
        if not reciprocal:
            candidate["reason_codes"].append("not_reciprocal_best")
        elif not unambiguous:
            candidate["reason_codes"].append("insufficient_best_match_margin")
        elif strict:
            candidate["reason_codes"].append("strict_reciprocal_best")

    return sorted(
        candidates,
        key=lambda item: (
            0 if item["candidate_tier"] == "strict" else 1,
            -item["match_score"], item["conference_paper_id"],
            item["journal_paper_id"],
        ),
    )


def load_family_papers(conn) -> list[dict]:
    latest_runs_sql = (
        "SELECT MAX(id) FROM serdes_screening_runs "
        "WHERE status='complete' AND scope_name LIKE 'all%' GROUP BY venue"
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT p.id, p.article_number, p.title, p.authors, "
            "CAST(p.year AS UNSIGNED) year, p.source_name, p.source_type, "
            "medium.link_medium, impl.implementation_id "
            "FROM serdes_paper_screenings screening "
            "JOIN papers p ON p.id=screening.paper_id "
            "LEFT JOIN serdes_paper_link_media medium ON medium.paper_id=p.id "
            "LEFT JOIN ("
            " SELECT ip.paper_id, COALESCE("
            "   MIN(CASE WHEN i.canonical_paper_id=ip.paper_id "
            "            THEN ip.implementation_id END),"
            "   MIN(ip.implementation_id)) implementation_id "
            " FROM serdes_implementation_papers ip "
            " JOIN serdes_implementations i ON i.id=ip.implementation_id "
            " GROUP BY ip.paper_id"
            ") impl ON impl.paper_id=p.id "
            f"WHERE screening.run_id IN ({latest_runs_sql}) "
            "AND screening.include_in_survey=1 "
            "AND p.source_system='ieee'"
        )
        return list(cur.fetchall())


def _existing_candidates(
    conn, *, for_update: bool = False,
) -> dict[tuple[int, int], dict]:
    with conn.cursor() as cur:
        suffix = " ORDER BY id FOR UPDATE" if for_update else ""
        cur.execute("SELECT * FROM serdes_implementation_match_candidates" + suffix)
        return {
            (int(row["conference_paper_id"]), int(row["journal_paper_id"])): row
            for row in cur.fetchall()
        }


def _record_decision_events(conn, events: list[tuple]) -> None:
    if not events:
        return
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO serdes_implementation_match_decision_events "
            "(candidate_id, previous_decision_status, decision_status, "
            " decision_source, decision_note, decided_by, decided_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            events,
        )


def _store_candidates(conn, candidates: list[dict], now: datetime) -> dict:
    # Lock the whole candidate decision set before applying sticky manual state.
    # This serializes builders and ensures a concurrent reviewer cannot be
    # overwritten between the read and the write.
    existing = _existing_candidates(conn, for_update=True)
    stored = {
        "inserted": 0, "updated": 0, "preserved_decisions": 0,
        "stale_auto_demoted": 0,
    }
    sql = """
        INSERT INTO serdes_implementation_match_candidates
            (candidate_key, conference_paper_id, journal_paper_id,
             conference_implementation_id, journal_implementation_id,
             candidate_tier, decision_status, decision_source, match_score,
             title_similarity, title_token_jaccard, shared_author_count,
             author_jaccard, first_author_match, year_gap, technical_matches,
             technical_conflicts, reason_codes, classifier_version,
             decision_note, decided_by, decided_at, first_seen_at, last_seen_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            conference_implementation_id=VALUES(conference_implementation_id),
            journal_implementation_id=VALUES(journal_implementation_id),
            candidate_tier=VALUES(candidate_tier),
            decision_status=VALUES(decision_status),
            decision_source=VALUES(decision_source),
            match_score=VALUES(match_score),
            title_similarity=VALUES(title_similarity),
            title_token_jaccard=VALUES(title_token_jaccard),
            shared_author_count=VALUES(shared_author_count),
            author_jaccard=VALUES(author_jaccard),
            first_author_match=VALUES(first_author_match),
            year_gap=VALUES(year_gap),
            technical_matches=VALUES(technical_matches),
            technical_conflicts=VALUES(technical_conflicts),
            reason_codes=VALUES(reason_codes),
            classifier_version=VALUES(classifier_version),
            decision_note=VALUES(decision_note),
            decided_by=VALUES(decided_by),
            decided_at=VALUES(decided_at),
            last_seen_at=VALUES(last_seen_at)
    """
    payloads = []
    decision_events: list[tuple] = []
    seen_pairs: set[tuple[int, int]] = set()
    for candidate in candidates:
        pair = (
            candidate["conference_paper_id"], candidate["journal_paper_id"],
        )
        seen_pairs.add(pair)
        prior = existing.get(pair)
        proposed = candidate["proposed_decision_status"]
        decision_status = resolve_decision_status(
            prior.get("decision_status") if prior else None,
            proposed,
            prior.get("decision_source") if prior else None,
        )
        sticky = bool(prior and decision_is_sticky(
            prior.get("decision_status"), prior.get("decision_source"),
        ))
        same_decision = bool(
            prior
            and decision_status == str(prior.get("decision_status") or "").lower()
        )
        preserve = bool(prior and (sticky or same_decision))
        decision_source = (
            prior.get("decision_source") if preserve else "classifier"
        ) or "classifier"
        decided_at = prior.get("decided_at") if preserve else now
        decision_note = prior.get("decision_note") if preserve else None
        decided_by = prior.get("decided_by") if preserve else None
        first_seen = prior.get("first_seen_at") if prior else now
        stored["updated" if prior else "inserted"] += 1
        if sticky:
            stored["preserved_decisions"] += 1
        if prior and (
            str(prior.get("decision_status") or "") != decision_status
            or str(prior.get("decision_source") or "") != decision_source
        ):
            decision_events.append((
                int(prior["id"]), prior.get("decision_status"), decision_status,
                decision_source, decision_note, decided_by, decided_at,
            ))
        payloads.append((
            candidate["candidate_key"], candidate["conference_paper_id"],
            candidate["journal_paper_id"],
            candidate.get("conference_implementation_id"),
            candidate.get("journal_implementation_id"),
            candidate["candidate_tier"], decision_status, decision_source,
            candidate["match_score"], candidate["title_similarity"],
            candidate["title_token_jaccard"],
            candidate["shared_author_count"], candidate["author_jaccard"],
            candidate["first_author_match"], candidate["year_gap"],
            json.dumps(candidate["technical_matches"], ensure_ascii=False),
            json.dumps(candidate["technical_conflicts"], ensure_ascii=False),
            json.dumps(candidate["reason_codes"], ensure_ascii=False),
            IMPLEMENTATION_FAMILY_VERSION, decision_note, decided_by,
            decided_at, first_seen, now,
        ))
    if payloads:
        with conn.cursor() as cur:
            cur.executemany(sql, payloads)
    stale = [
        row for pair, row in existing.items()
        if pair not in seen_pairs
        and str(row.get("decision_status") or "") == "auto_grouped"
        and str(row.get("decision_source") or "") == "classifier"
    ]
    if stale:
        stale_note = (
            "Released by classifier: pair was not produced by the current "
            "candidate build. Manual review is required before regrouping."
        )
        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE serdes_implementation_match_candidates SET "
                "candidate_tier='manual_review', decision_status='manual_review', "
                "decision_source='classifier', classifier_version=%s, "
                "decision_note=%s, decided_by=NULL, decided_at=%s "
                "WHERE id=%s AND decision_status='auto_grouped' "
                "AND decision_source='classifier'",
                [
                    (IMPLEMENTATION_FAMILY_VERSION, stale_note, now, int(row["id"]))
                    for row in stale
                ],
            )
        decision_events.extend(
            (
                int(row["id"]), "auto_grouped", "manual_review", "classifier",
                stale_note, None, now,
            )
            for row in stale
        )
        stored["stale_auto_demoted"] = len(stale)
    _record_decision_events(conn, decision_events)
    return stored


def _lock_family_state(conn) -> tuple[list[dict], list[dict], list[dict]]:
    """Lock the decision projection in one fixed order for the transaction."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM serdes_implementation_match_candidates "
            "ORDER BY id FOR UPDATE"
        )
        candidates = list(cur.fetchall())
        cur.execute(
            "SELECT * FROM serdes_implementation_families "
            "ORDER BY id FOR UPDATE"
        )
        families = list(cur.fetchall())
        cur.execute(
            "SELECT * FROM serdes_implementation_family_members "
            "ORDER BY family_id, paper_id FOR UPDATE"
        )
        members = list(cur.fetchall())
    return candidates, families, members


def _create_families(conn, now: datetime) -> dict:
    del now  # timestamps are maintained by MySQL; retained for API stability.
    candidates, families, members = _lock_family_state(conn)
    selected, conflicts = select_family_candidates(candidates)

    # A paper cannot simultaneously be another family's canonical journal and
    # a conference version.  This should not occur with the venue allow-list,
    # but rejecting it here keeps future venue additions safe.
    canonical_papers = {int(row["journal_paper_id"]) for row in selected}
    safe_selected: list[dict] = []
    for row in selected:
        conference_id = int(row["conference_paper_id"])
        journal_id = int(row["journal_paper_id"])
        if conference_id == journal_id or conference_id in canonical_papers:
            conflicts.append(row)
        else:
            safe_selected.append(row)
    selected = safe_selected

    stats = {
        "families_created": 0,
        "families_deactivated": 0,
        "candidates_grouped": 0,
        "candidates_ungrouped": 0,
        "already_grouped": 0,
        "membership_conflicts": len(conflicts),
        "members_deleted": 0,
        "members_inserted": 0,
        "members_resynced": 0,
    }
    family_by_canonical = {
        int(row["canonical_paper_id"]): int(row["id"]) for row in families
    }
    family_row_by_id = {int(row["id"]): row for row in families}
    selected_by_journal: dict[int, list[dict]] = defaultdict(list)
    for row in selected:
        selected_by_journal[int(row["journal_paper_id"])].append(row)

    # Reuse stable families by canonical journal and create only missing rows.
    for journal_id in sorted(selected_by_journal):
        if journal_id in family_by_canonical:
            continue
        journal_candidates = selected_by_journal[journal_id]
        manual = any(
            str(row.get("decision_status")) == "approved"
            for row in journal_candidates
        )
        method = (
            "manual_conference_journal" if manual
            else "strict_reciprocal_best"
        )
        confidence = max(float(row["match_score"]) for row in journal_candidates)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO serdes_implementation_families "
                "(family_key, canonical_paper_id, family_method, "
                " family_confidence, classifier_version, review_status) "
                "VALUES (%s,%s,%s,%s,%s,'active')",
                (
                    family_key(journal_id), journal_id, method, confidence,
                    IMPLEMENTATION_FAMILY_VERSION,
                ),
            )
            family_id = int(cur.lastrowid)
        family_by_canonical[journal_id] = family_id
        family_row_by_id[family_id] = {
            "id": family_id,
            "canonical_paper_id": journal_id,
            "review_status": "active",
        }
        stats["families_created"] += 1

    desired_members: dict[int, dict] = {}
    assignments: dict[int, int] = {}
    grouped_by_family: dict[int, list[dict]] = defaultdict(list)
    final_selected: list[dict] = []
    for row in selected:
        candidate_id = int(row["id"])
        conference_id = int(row["conference_paper_id"])
        journal_id = int(row["journal_paper_id"])
        family_id = family_by_canonical[journal_id]
        journal_member = desired_members.get(journal_id)
        conference_member = desired_members.get(conference_id)
        if (
            (journal_member and int(journal_member["family_id"]) != family_id)
            or conference_member is not None
        ):
            conflicts.append(row)
            stats["membership_conflicts"] += 1
            continue

        if journal_member is None:
            desired_members[journal_id] = {
                "family_id": family_id,
                "paper_id": journal_id,
                "implementation_id": row.get("journal_implementation_id"),
                "match_candidate_id": None,
                "relation_type": "canonical_journal",
                "is_canonical": 1,
            }
        elif (
            journal_member.get("implementation_id") is None
            and row.get("journal_implementation_id") is not None
        ):
            journal_member["implementation_id"] = row[
                "journal_implementation_id"
            ]
        desired_members[conference_id] = {
            "family_id": family_id,
            "paper_id": conference_id,
            "implementation_id": row.get("conference_implementation_id"),
            "match_candidate_id": candidate_id,
            "relation_type": "conference_version",
            "is_canonical": 0,
        }
        assignments[candidate_id] = family_id
        grouped_by_family[family_id].append(row)
        final_selected.append(row)

    old_member_by_paper = {int(row["paper_id"]): row for row in members}
    stale_members = []
    for row in members:
        paper_id = int(row["paper_id"])
        desired = desired_members.get(paper_id)
        if desired is None or int(desired["family_id"]) != int(row["family_id"]):
            stale_members.append((int(row["family_id"]), paper_id))
            old_member_by_paper.pop(paper_id, None)
    if stale_members:
        with conn.cursor() as cur:
            cur.executemany(
                "DELETE FROM serdes_implementation_family_members "
                "WHERE family_id=%s AND paper_id=%s",
                stale_members,
            )
        stats["members_deleted"] = len(stale_members)

    # Sync every desired member, including already-grouped pairs.  A newly
    # discovered implementation id replaces a stale/non-null value; NULL only
    # preserves a previously known association.
    inserts: list[tuple] = []
    updates: list[tuple] = []
    for paper_id in sorted(desired_members):
        desired = desired_members[paper_id]
        existing = old_member_by_paper.get(paper_id)
        if existing is None:
            inserts.append((
                desired["family_id"], paper_id, desired["implementation_id"],
                desired["match_candidate_id"], desired["relation_type"],
                desired["is_canonical"],
            ))
        else:
            updates.append((
                desired["implementation_id"], desired["match_candidate_id"],
                desired["relation_type"], desired["is_canonical"],
                desired["family_id"], paper_id,
            ))
    with conn.cursor() as cur:
        if inserts:
            cur.executemany(
                "INSERT INTO serdes_implementation_family_members "
                "(family_id, paper_id, implementation_id, match_candidate_id, "
                " relation_type, is_canonical) VALUES (%s,%s,%s,%s,%s,%s)",
                inserts,
            )
        if updates:
            cur.executemany(
                "UPDATE serdes_implementation_family_members SET "
                "implementation_id=COALESCE(%s, implementation_id), "
                "match_candidate_id=%s, relation_type=%s, is_canonical=%s "
                "WHERE family_id=%s AND paper_id=%s",
                updates,
            )
    stats["members_inserted"] = len(inserts)
    stats["members_resynced"] = len(updates)

    prior_family_by_candidate = {
        int(row["id"]): (
            int(row["family_id"]) if row.get("family_id") is not None else None
        )
        for row in candidates
    }
    stats["candidates_ungrouped"] = sum(
        prior_family is not None and candidate_id not in assignments
        for candidate_id, prior_family in prior_family_by_candidate.items()
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE serdes_implementation_match_candidates "
            "SET family_id=NULL WHERE family_id IS NOT NULL"
        )
        if assignments:
            cur.executemany(
                "UPDATE serdes_implementation_match_candidates "
                "SET family_id=%s WHERE id=%s",
                [
                    (family_id, candidate_id)
                    for candidate_id, family_id in sorted(assignments.items())
                ],
            )

    old_members = {int(row["paper_id"]): row for row in members}
    for row in final_selected:
        candidate_id = int(row["id"])
        family_id = assignments[candidate_id]
        conference_member = old_members.get(int(row["conference_paper_id"]))
        journal_member = old_members.get(int(row["journal_paper_id"]))
        already = (
            prior_family_by_candidate.get(candidate_id) == family_id
            and conference_member is not None
            and int(conference_member["family_id"]) == family_id
            and journal_member is not None
            and int(journal_member["family_id"]) == family_id
        )
        stats["already_grouped" if already else "candidates_grouped"] += 1

    active_family_ids = set(grouped_by_family)
    for family_id, family in sorted(family_row_by_id.items()):
        family_candidates = grouped_by_family.get(family_id, [])
        if not family_candidates:
            if str(family.get("review_status")) == "active":
                stats["families_deactivated"] += 1
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE serdes_implementation_families SET "
                    "review_status='inactive', classifier_version=%s WHERE id=%s",
                    (IMPLEMENTATION_FAMILY_VERSION, family_id),
                )
            continue
        manual = any(
            str(row.get("decision_status")) == "approved"
            for row in family_candidates
        )
        method = (
            "manual_conference_journal" if manual
            else "strict_reciprocal_best"
        )
        confidence = max(float(row["match_score"]) for row in family_candidates)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE serdes_implementation_families SET "
                "family_method=%s, family_confidence=%s, classifier_version=%s, "
                "review_status='active' WHERE id=%s",
                (
                    method, confidence, IMPLEMENTATION_FAMILY_VERSION,
                    family_id,
                ),
            )
    stats["active_family_count"] = len(active_family_ids)
    return stats


def _assert_family_invariants(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, canonical_paper_id, review_status "
            "FROM serdes_implementation_families ORDER BY id"
        )
        families = list(cur.fetchall())
        cur.execute(
            "SELECT family_id, paper_id, implementation_id, "
            "match_candidate_id, relation_type, is_canonical "
            "FROM serdes_implementation_family_members "
            "ORDER BY family_id, paper_id"
        )
        members = list(cur.fetchall())
        cur.execute(
            "SELECT id, conference_paper_id, journal_paper_id, "
            "conference_implementation_id, journal_implementation_id, "
            "family_id, decision_status "
            "FROM serdes_implementation_match_candidates ORDER BY id"
        )
        candidates = list(cur.fetchall())
    violations = validate_family_snapshot(families, members, candidates)
    if violations:
        sample = "; ".join(violations[:8])
        raise RuntimeError(
            f"implementation family invariant failure ({len(violations)}): {sample}"
        )
    return {
        "family_invariant_violations": 0,
        "active_family_total": sum(
            str(row.get("review_status")) == "active" for row in families
        ),
        "grouped_candidate_total": sum(
            row.get("family_id") is not None for row in candidates
        ),
    }


def build_implementation_families(conn, dry_run: bool = False) -> dict:
    """Persist candidates and strict families without changing measurements."""
    papers = load_family_papers(conn)
    candidates = build_match_candidates(papers)
    stats = {
        "papers": len(papers),
        "candidates": len(candidates),
        "strict_candidates": sum(
            item["candidate_tier"] == "strict" for item in candidates
        ),
        "manual_review_candidates": sum(
            item["candidate_tier"] == "manual_review" for item in candidates
        ),
        "classifier_version": IMPLEMENTATION_FAMILY_VERSION,
        "dry_run": bool(dry_run),
    }
    if dry_run:
        return stats

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        stats.update(_store_candidates(conn, candidates, now))
        stats.update(_create_families(conn, now))
        stats.update(_assert_family_invariants(conn))
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) total FROM serdes_implementation_families")
            stats["family_total"] = int(cur.fetchone()["total"])
            cur.execute(
                "SELECT COUNT(*) total FROM serdes_implementation_family_members"
            )
            stats["family_member_total"] = int(cur.fetchone()["total"])
            cur.execute(
                "SELECT decision_status, COUNT(*) total "
                "FROM serdes_implementation_match_candidates "
                "GROUP BY decision_status"
            )
            stats["decision_counts"] = {
                row["decision_status"]: int(row["total"])
                for row in cur.fetchall()
            }
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return stats


def set_match_decision(
    conn,
    candidate_id: int,
    decision_status: str,
    *,
    decided_by: str,
    decision_note: str | None = None,
) -> dict:
    """Apply one manual decision and reconcile derived families atomically.

    Callers should use this function instead of updating the candidate row
    directly.  ``rejected`` and ``manual_review`` remove the candidate's family
    membership before commit, so API joins cannot retain stale deduplication.
    """
    status = str(decision_status or "").strip().lower()
    if status not in MANUAL_DECISIONS:
        raise ValueError(
            "manual decision_status must be manual_review, approved, or rejected"
        )
    reviewer = str(decided_by or "").strip()
    if not reviewer:
        raise ValueError("decided_by is required for a manual decision")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        existing = _existing_candidates(conn, for_update=True)
        row = next(
            (
                candidate for candidate in existing.values()
                if int(candidate["id"]) == int(candidate_id)
            ),
            None,
        )
        if row is None:
            raise KeyError(f"unknown implementation match candidate {candidate_id}")
        _record_decision_events(conn, [(
            int(candidate_id), row.get("decision_status"), status, "manual",
            decision_note, reviewer, now,
        )])
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE serdes_implementation_match_candidates SET "
                "decision_status=%s, decision_source='manual', decision_note=%s, "
                "decided_by=%s, decided_at=%s WHERE id=%s",
                (status, decision_note, reviewer, now, int(candidate_id)),
            )
        stats = {
            "candidate_id": int(candidate_id),
            "previous_decision_status": row.get("decision_status"),
            "decision_status": status,
            "decision_source": "manual",
        }
        stats.update(_create_families(conn, now))
        stats.update(_assert_family_invariants(conn))
        conn.commit()
        return stats
    except Exception:
        conn.rollback()
        raise
