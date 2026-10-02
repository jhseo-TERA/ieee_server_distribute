"""Build a deterministic, non-destructive SerDes metric review queue.

The queue is deliberately separate from extraction and database mutation.  It
accepts plain dictionaries, emits plain dictionaries, and keeps every proposed
review action next to the unchanged source measurement.  The CLI wrapper reads
the fixed Core + Adjacent corpus and writes JSON/CSV artifacts only.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import math
import re
from statistics import median
from typing import Any, Iterable

from serdes_enrichment import (
    VALID_ENERGY_COMPONENT_SCOPES,
    VALID_LINK_SUBTYPES,
    classify_energy_component_scope,
    classify_link_subtype,
)
from serdes_metrics import canonical_ieee_url, classify_link_medium, plain_text


REVIEW_RULE_VERSION = "serdes-review-queue-1.0"
CALCULATED_ENERGY_BASIS = "calculated_power_per_rate"
VALID_REVIEW_STATUSES = {"pending", "keep", "correct", "not_comparable", "exclude", "defer"}
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}

SOURCE_PRIORITY = {
    "manual": 60,
    "user_sheet": 55,
    "pdf": 50,
    "reference_xlsx": 40,
    "abstract": 30,
    "title": 10,
}
REVIEW_PRIORITY = {
    "verified": 4,
    "reviewed": 3,
    "curated_reference": 2,
    "extracted": 1,
}

RATE_FIELDS = (
    "lane_rate_gbps", "aggregate_rate_gbps", "reported_rate_gbps",
    "reported_rate_min_gbps", "reported_rate_max_gbps", "symbol_rate_gbaud",
)
CORE_NUMERIC_FIELDS = (
    "energy_pj_bit", "lane_rate_gbps", "reported_rate_gbps", "process_nm",
    "channel_loss_db",
)
ORIGINAL_MEASUREMENT_FIELDS = (
    "measurement_id", "implementation_id", "operating_point_key",
    "source_kind", "review_status", "overall_confidence",
    "reported_rate_text", "reported_rate_gbps", "reported_rate_min_gbps",
    "reported_rate_max_gbps", "rate_scope", "lane_rate_gbps", "lane_count",
    "aggregate_rate_gbps", "aggregate_rate_basis", "symbol_rate_gbaud",
    "throughput_density_gbps_per_mm", "power_mw", "power_scope",
    "energy_pj_bit", "energy_scope", "energy_basis",
    "energy_loss_normalized_pj_bit_db", "process_text", "process_nm",
    "channel_loss_db", "loss_frequency_ghz", "ber", "ber_scope",
    "active_area_mm2", "link_class", "component_scope", "modulation",
)

NORMALIZED_ENERGY_RE = re.compile(
    r"(?:a|f|p|n|u|µ|μ)?j\s*/\s*(?:bit|b)\s*/\s*(?:mm|db)\b",
    re.IGNORECASE,
)
PLAIN_ENERGY_RE = re.compile(
    r"(?:a|f|p|n|u|µ|μ)?j\s*/\s*(?:bit|b)(?![a-z]|\s*/)",
    re.IGNORECASE,
)
REFERENCE_CONTEXT_RE = re.compile(
    r"(?:\b(?:in|from|of|by)\s*\[\d+\]|previous|prior work|reported in|"
    r"demonstrated in|compared with)",
    re.IGNORECASE,
)
REFERENCE_SENSITIVE_FIELDS = {
    "energy_pj_bit", "reported_rate_gbps", "power_mw", "process_nm",
    "channel_loss_db", "ber",
}


RULES = (
    {
        "code": "FOM_CROSSCHECK_CONFLICT", "family": "fom_consistency", "priority": "P0",
        "description": "Reported energy disagrees with same-point Power/Rate beyond max(0.01 pJ/b, 2%).",
        "action": "Confirm rate/power scope and retain the reported value until reviewed.",
    },
    {
        "code": "ENERGY_POWER_RATE_CONFLICT", "family": "fom_consistency", "priority": "P0",
        "description": "Known-scope Power/Rate recomputation disagrees with stored energy.",
        "action": "Check the denominator and whether power is lane or aggregate scope.",
    },
    {
        "code": "NORMALIZED_UNIT_AS_ENERGY", "family": "unit_collision", "priority": "P0",
        "description": "Energy evidence contains /mm or /dB but no independent plain pJ/bit value.",
        "action": "Move the value to its normalized FoM field or mark it not comparable.",
    },
    {
        "code": "REFERENCE_CONTEXT_VALUE", "family": "reference_context", "priority": "P0",
        "description": "A numeric value was extracted from language that may describe cited/prior work.",
        "action": "Verify that the metric belongs to this paper's implementation.",
    },
    {
        "code": "AGGREGATE_LANE_MISMATCH", "family": "rate_consistency", "priority": "P0",
        "description": "Aggregate rate differs from lane rate × lane count by more than max(1 Gb/s, 2%).",
        "action": "Correct the lane count/rate or annotate a non-uniform aggregate definition.",
    },
    {
        "code": "RATE_RANGE_CONFLICT", "family": "rate_consistency", "priority": "P0",
        "description": "Rate range is reversed or the fixed reported rate lies outside the stored range.",
        "action": "Verify the operating point and range endpoints.",
    },
    {
        "code": "DOMAIN_VALUE_INVALID", "family": "domain_integrity", "priority": "P0",
        "description": "A physical value is non-positive or outside a hard semantic domain.",
        "action": "Correct the unit/value before charting.",
    },
    {
        "code": "ENERGY_OUTSIDE_HARD_GUARD", "family": "energy_extreme", "priority": "P0",
        "description": "Energy lies outside the derivation guard [0.001, 1000] pJ/bit.",
        "action": "Confirm units and system/component scope; do not auto-delete a reported value.",
    },
    {
        "code": "ENERGY_SOFT_EXTREME", "family": "energy_extreme", "priority": "P1",
        "description": "Energy is below 0.01 or above 100 pJ/bit.",
        "action": "Review as a plausible historical/scope outlier before comparison.",
    },
    {
        "code": "ENERGY_SCOPE_UNKNOWN", "family": "scope_gap", "priority": "P1",
        "description": "Energy evidence explicitly indicates a partial/excluded block, so circuit coverage remains unknown.",
        "action": "Assign the covered block from the source or mark this value not comparable; generic missing scope stays in summary only.",
    },
    {
        "code": "ROBUST_ENERGY_OUTLIER", "family": "statistical_outlier", "priority": "P1",
        "description": "Within medium + energy scope (n≥8), |modified z| exceeds 3.5 in log10 energy.",
        "action": "Verify units, attribution and scope; statistical rarity alone is not an error.",
    },
    {
        "code": "NUMERIC_WITHOUT_EVIDENCE", "family": "evidence_gap", "priority": "P1",
        "description": "A core numeric metric has no linked evidence row.",
        "action": "Attach source evidence or downgrade the metric.",
    },
)
RULE_BY_CODE = {item["code"]: item for item in RULES}


DATA_DICTIONARY = (
    ("queue_id", "Review Queue", "Stable paper + flag-family identifier."),
    ("sequence", "Review Queue", "Deterministic review order."),
    ("batch_no", "Review Queue", "50-row daily batch number by default."),
    ("batch_item", "Review Queue", "Position within the daily batch."),
    ("priority", "Both", "P0 deterministic/high-risk; P1 contextual/statistical."),
    ("flag_family", "Both", "Grouping dimension; one queue row per paper + family."),
    ("flag_codes", "Review Queue", "All rule codes represented by the queue row."),
    ("review_status", "Review Queue", "Reviewer-owned state; defaults to pending."),
    ("reviewer_value", "Review Queue", "Optional corrected value; never overwrites originals."),
    ("reviewer_scope", "Review Queue", "Optional reviewer-assigned circuit/rate scope."),
    ("reviewer_note", "Review Queue", "Free-form review note."),
    ("measurement_id", "Measurement Flags", "Immutable SQL measurement identifier."),
    ("implementation_id", "Measurement Flags", "Implementation identifier used for deduplication."),
    ("actual_value", "Both", "Stored value implicated by the rule."),
    ("expected_value", "Both", "Computed comparison value or applicable guard boundary."),
    ("delta", "Both", "actual - expected when numeric comparison is meaningful."),
    ("group_median", "Both", "Geometric median energy for a robust comparison group."),
    ("robust_z", "Both", "Modified z-score in log10 energy."),
    ("energy_component_scope", "Both", "TX/RX/TRX/full-link/driver-only/unknown circuit coverage."),
    ("link_medium", "Both", "Electrical, optical, or unspecified physical medium."),
    ("link_subtype", "Both", "Medium-specific topology/category classification."),
    ("evidence_ids", "Measurement Flags", "Evidence rows that triggered the flag."),
    ("evidence_excerpts", "Measurement Flags", "Verbatim local evidence snippets for review."),
    ("original_values", "Measurement Flags", "Unchanged source measurement fields as JSON."),
    ("rule_version", "Both", "Version of the deterministic review rules."),
)


def json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_value(item) for item in value]
    return value


def number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _reason_code_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
        if value.strip():
            return [value.strip()]
    return []


def _stable_id(*values: Any) -> str:
    payload = "|".join(str(value or "") for value in values)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _paper_metadata(row: dict) -> dict:
    return {
        "paper_id": row.get("paper_id"),
        "article_number": row.get("article_number"),
        "doi": row.get("doi"),
        "title": row.get("title"),
        "authors": row.get("authors"),
        "year": row.get("year"),
        "venue": row.get("venue"),
        "citation_count": row.get("citation_count"),
        "pdf_available": bool(row.get("pdf_available")),
        "ieee_url": canonical_ieee_url(row.get("article_number"), row.get("paper_url")),
    }


def _enrich_measurement(row: dict, evidence: list[dict]) -> dict:
    item = {key: json_value(value) for key, value in row.items()}
    item["measurement_id"] = int(item.get("measurement_id") or item.get("id"))
    item["implementation_id"] = int(item["implementation_id"])
    item["paper_id"] = int(item["paper_id"])
    item["evidence_count"] = int(item.get("evidence_count") or len(evidence))

    medium = str(item.get("link_medium") or "").strip().lower()
    if medium not in {"electrical", "optical", "unspecified"}:
        medium_decision = classify_link_medium(
            item.get("title"), item.get("paper_abstract"),
            [item.get("link_class")] if item.get("link_class") else None,
            screening_class=item.get("relevance_class"),
        )
        medium = medium_decision["link_medium"]
    item["link_medium"] = medium

    metric_evidence = [
        ev for ev in evidence if ev.get("field_name") in {"energy_pj_bit", "power_mw"}
    ]
    metric_evidence.sort(key=lambda ev: (
        int(ev.get("source_kind") == item.get("source_kind")),
        int(str(ev.get("review_status") or "") in {"verified", "reviewed", "calculated"}),
        number(ev.get("confidence")) or 0,
        -int(ev.get("evidence_id") or ev.get("id") or 0),
    ), reverse=True)
    stored_scope = str(item.get("energy_component_scope") or "").strip().lower()
    if stored_scope in VALID_ENERGY_COMPONENT_SCOPES:
        item["energy_component_scope"] = stored_scope
        item["scope_source"] = item.get("scope_source") or "stored"
        item["scope_confidence"] = number(item.get("scope_confidence")) or 0.0
        item["scope_reason_codes"] = _reason_code_list(
            item.get("scope_reason_codes")
        )
    else:
        scope = classify_energy_component_scope(
            item.get("title"), [ev.get("evidence_text") for ev in metric_evidence],
            item.get("component_scope"),
        )
        item.update({
            "energy_component_scope": scope["energy_component_scope"],
            "scope_source": scope["source"],
            "scope_confidence": scope["confidence"],
            "scope_reason_codes": scope["reason_codes"],
        })
    stored_subtype = str(item.get("link_subtype") or "").strip().lower()
    if stored_subtype in VALID_LINK_SUBTYPES:
        item["link_subtype"] = stored_subtype
        item["subtype_source"] = item.get("subtype_source") or "stored"
        item["subtype_confidence"] = number(item.get("subtype_confidence")) or 0.0
    else:
        subtype = classify_link_subtype(
            item.get("title"), item.get("paper_abstract"), medium,
            item.get("link_class"),
        )
        item.update({
            "link_subtype": subtype["link_subtype"],
            "subtype_source": subtype["source"],
            "subtype_confidence": subtype["confidence"],
        })
    return item


def _flag(
    row: dict,
    code: str,
    *,
    field: str | None = None,
    actual_value: Any = None,
    expected_value: Any = None,
    delta: Any = None,
    group_median: Any = None,
    robust_z: Any = None,
    message: str | None = None,
    evidence: Iterable[dict] = (),
) -> dict:
    rule = RULE_BY_CODE[code]
    evidence = list(evidence)
    evidence_ids = [int(ev.get("evidence_id") or ev.get("id")) for ev in evidence if ev.get("evidence_id") or ev.get("id")]
    evidence_texts = [plain_text(ev.get("evidence_text"))[:1000] for ev in evidence]
    original = {key: json_value(row.get(key)) for key in ORIGINAL_MEASUREMENT_FIELDS}
    result = {
        "flag_id": _stable_id(row["measurement_id"], code, field, ",".join(map(str, evidence_ids))),
        "measurement_id": row["measurement_id"],
        "implementation_id": row["implementation_id"],
        **_paper_metadata(row),
        "priority": rule["priority"],
        "flag_family": rule["family"],
        "flag_code": code,
        "field": field,
        "message": message or rule["description"],
        "recommended_action": rule["action"],
        "actual_value": json_value(actual_value),
        "expected_value": json_value(expected_value),
        "delta": json_value(delta),
        "group_median": json_value(group_median),
        "robust_z": json_value(robust_z),
        "link_medium": row.get("link_medium"),
        "link_subtype": row.get("link_subtype"),
        "energy_component_scope": row.get("energy_component_scope"),
        "scope_source": row.get("scope_source"),
        "scope_confidence": row.get("scope_confidence"),
        "scope_reason_codes": row.get("scope_reason_codes") or [],
        "source_kind": row.get("source_kind"),
        "measurement_review_status": row.get("review_status"),
        "overall_confidence": json_value(row.get("overall_confidence")),
        "evidence_count": int(row.get("evidence_count") or 0),
        "evidence_ids": evidence_ids,
        "evidence_fields": [ev.get("field_name") for ev in evidence],
        "evidence_source_urls": [ev.get("source_url") for ev in evidence if ev.get("source_url")],
        "evidence_locators": [ev.get("source_locator") for ev in evidence if ev.get("source_locator")],
        "evidence_excerpts": evidence_texts,
        "original_values": original,
        "rule_version": REVIEW_RULE_VERSION,
    }
    return result


def _domain_flags(row: dict) -> list[dict]:
    flags = []
    positive_fields = (
        "energy_pj_bit", "power_mw", "process_nm", "lane_rate_gbps",
        "aggregate_rate_gbps", "reported_rate_gbps", "symbol_rate_gbaud",
        "active_area_mm2",
    )
    invalid = [(field, number(row.get(field))) for field in positive_fields]
    invalid = [(field, value) for field, value in invalid if row.get(field) is not None and (value is None or value <= 0)]
    loss = number(row.get("channel_loss_db"))
    if row.get("channel_loss_db") is not None and (loss is None or loss < 0 or loss > 100):
        invalid.append(("channel_loss_db", loss))
    ber = number(row.get("ber"))
    if row.get("ber") is not None and (ber is None or not 0 < ber < 1):
        invalid.append(("ber", ber))
    for field, value in invalid:
        flags.append(_flag(
            row, "DOMAIN_VALUE_INVALID", field=field, actual_value=value,
            message=f"{field} is outside its hard physical domain.",
        ))
    return flags


def _row_flags(row: dict, evidence: list[dict]) -> list[dict]:
    flags = _domain_flags(row)
    energy = number(row.get("energy_pj_bit"))
    if energy is not None:
        if energy < 0.001 or energy > 1000:
            boundary = 0.001 if energy < 0.001 else 1000.0
            flags.append(_flag(
                row, "ENERGY_OUTSIDE_HARD_GUARD", field="energy_pj_bit",
                actual_value=energy, expected_value=boundary, delta=energy - boundary,
            ))
        if energy < 0.01 or energy > 100:
            boundary = 0.01 if energy < 0.01 else 100.0
            flags.append(_flag(
                row, "ENERGY_SOFT_EXTREME", field="energy_pj_bit",
                actual_value=energy, expected_value=boundary, delta=energy - boundary,
            ))
        # A generic unknown is a completeness gap, not automatically an
        # outlier.  Queue only explicit partial/exclusion evidence; report all
        # remaining unknowns in Summary so the review queue stays actionable.
        if (
            row.get("energy_component_scope") == "unknown"
            and "partial_scope_exclusion" in (row.get("scope_reason_codes") or ())
        ):
            flags.append(_flag(
                row, "ENERGY_SCOPE_UNKNOWN", field="energy_component_scope",
                actual_value="unknown",
            ))

    if any(row.get(field) is not None for field in CORE_NUMERIC_FIELDS) and not evidence:
        flags.append(_flag(row, "NUMERIC_WITHOUT_EVIDENCE"))

    lane = number(row.get("lane_rate_gbps"))
    lanes = number(row.get("lane_count"))
    aggregate = number(row.get("aggregate_rate_gbps"))
    if lane and lanes and aggregate:
        expected = lane * lanes
        delta = aggregate - expected
        if abs(delta) > max(1.0, 0.02 * aggregate):
            flags.append(_flag(
                row, "AGGREGATE_LANE_MISMATCH", field="aggregate_rate_gbps",
                actual_value=aggregate, expected_value=expected, delta=delta,
            ))

    rate_min = number(row.get("reported_rate_min_gbps"))
    rate_max = number(row.get("reported_rate_max_gbps"))
    reported = number(row.get("reported_rate_gbps"))
    if rate_min is not None and rate_max is not None:
        if rate_min > rate_max:
            flags.append(_flag(
                row, "RATE_RANGE_CONFLICT", field="reported_rate_range",
                actual_value=[rate_min, rate_max], expected_value="min <= max",
            ))
        elif reported is not None and not rate_min <= reported <= rate_max:
            expected = min(max(reported, rate_min), rate_max)
            flags.append(_flag(
                row, "RATE_RANGE_CONFLICT", field="reported_rate_gbps",
                actual_value=reported, expected_value=expected, delta=reported - expected,
            ))

    crosschecks = [
        ev for ev in evidence
        if ev.get("field_name") == "energy_pj_bit_crosscheck"
        and str(ev.get("review_status") or "").lower() == "needs_review"
    ]
    if crosschecks:
        flags.append(_flag(
            row, "FOM_CROSSCHECK_CONFLICT", field="energy_pj_bit",
            actual_value=energy, evidence=crosschecks,
        ))
    elif energy is not None:
        power = number(row.get("power_mw"))
        power_scope = str(row.get("power_scope") or "unknown").lower()
        rate_scope = str(row.get("rate_scope") or "unknown").lower()
        denominator = None
        if power_scope == "lane":
            denominator = lane or (reported if rate_scope == "lane" else None)
        elif power_scope == "aggregate":
            denominator = aggregate or (reported if rate_scope == "aggregate" else None)
        if power and denominator:
            expected = power / denominator
            delta = energy - expected
            if abs(delta) > max(0.01, 0.02 * energy):
                flags.append(_flag(
                    row, "ENERGY_POWER_RATE_CONFLICT", field="energy_pj_bit",
                    actual_value=energy, expected_value=expected, delta=delta,
                ))

    unit_rows = [
        ev for ev in evidence
        if ev.get("field_name") == "energy_pj_bit"
        and str(ev.get("extraction_method") or "").lower() == "regex"
        and NORMALIZED_ENERGY_RE.search(plain_text(ev.get("evidence_text")))
        and not PLAIN_ENERGY_RE.search(plain_text(ev.get("evidence_text")))
    ]
    if unit_rows:
        flags.append(_flag(
            row, "NORMALIZED_UNIT_AS_ENERGY", field="energy_pj_bit",
            actual_value=energy, evidence=unit_rows,
        ))

    reference_rows = [
        ev for ev in evidence
        if ev.get("field_name") in REFERENCE_SENSITIVE_FIELDS
        and str(ev.get("source_kind") or "").lower() == "abstract"
        and str(ev.get("extraction_method") or "").lower() == "regex"
        and REFERENCE_CONTEXT_RE.search(plain_text(ev.get("evidence_text")))
    ]
    if reference_rows:
        flags.append(_flag(
            row, "REFERENCE_CONTEXT_VALUE",
            field=", ".join(sorted({str(ev.get("field_name")) for ev in reference_rows})),
            evidence=reference_rows,
        ))
    return flags


def _measurement_rank(row: dict) -> tuple:
    populated = sum(row.get(field) is not None for field in CORE_NUMERIC_FIELDS + RATE_FIELDS)
    reported_rank = int(row.get("energy_basis") != CALCULATED_ENERGY_BASIS)
    return (
        REVIEW_PRIORITY.get(str(row.get("review_status") or "").lower(), 0),
        reported_rank,
        SOURCE_PRIORITY.get(str(row.get("source_kind") or "").lower(), 0),
        populated,
        number(row.get("overall_confidence")) or 0,
        int(row.get("evidence_count") or 0),
        str(row.get("updated_at") or ""),
        int(row.get("measurement_id") or 0),
    )


def _representative_energy_rows(rows: Iterable[dict]) -> list[dict]:
    best = {}
    for row in rows:
        if number(row.get("energy_pj_bit")) is None:
            continue
        implementation_id = int(row["implementation_id"])
        if implementation_id not in best or _measurement_rank(row) > _measurement_rank(best[implementation_id]):
            best[implementation_id] = row
    return list(best.values())


def _robust_flags(rows: Iterable[dict], min_group_size: int = 8) -> list[dict]:
    groups = defaultdict(list)
    for row in _representative_energy_rows(rows):
        energy = number(row.get("energy_pj_bit"))
        if energy is None or energy <= 0:
            continue
        group = (row.get("link_medium") or "unspecified", row.get("energy_component_scope") or "unknown")
        groups[group].append((math.log10(energy), row))

    flags = []
    for group, values in sorted(groups.items(), key=lambda item: str(item[0])):
        if len(values) < min_group_size:
            continue
        logs = [value for value, _ in values]
        center = median(logs)
        mad = median(abs(value - center) for value in logs)
        if mad <= 0:
            continue
        geometric_median = 10 ** center
        for value, row in values:
            robust_z = 0.6745 * (value - center) / mad
            if abs(robust_z) <= 3.5:
                continue
            flags.append(_flag(
                row, "ROBUST_ENERGY_OUTLIER", field="energy_pj_bit",
                actual_value=number(row.get("energy_pj_bit")),
                group_median=geometric_median, robust_z=robust_z,
                message=(
                    f"log10 energy is a robust outlier in {group[0]} / {group[1]} "
                    f"(n={len(values)}, modified z={robust_z:.3f})."
                ),
            ))
    return flags


def _queue_rows(flags: list[dict], batch_size: int) -> list[dict]:
    grouped = defaultdict(list)
    for flag in flags:
        grouped[(int(flag["paper_id"]), flag["flag_family"])].append(flag)
    rows = []
    for (paper_id, family), details in grouped.items():
        details.sort(key=lambda item: (
            PRIORITY_ORDER[item["priority"]], item["flag_code"], item["measurement_id"],
        ))
        primary = details[0]
        priorities = sorted({item["priority"] for item in details}, key=PRIORITY_ORDER.get)
        actual_values = [item["actual_value"] for item in details if item.get("actual_value") is not None]
        expected_values = [item["expected_value"] for item in details if item.get("expected_value") is not None]
        deltas = [number(item.get("delta")) for item in details]
        deltas = [value for value in deltas if value is not None]
        robust_values = [number(item.get("robust_z")) for item in details]
        robust_values = [value for value in robust_values if value is not None]
        median_values = [number(item.get("group_median")) for item in details]
        median_values = [value for value in median_values if value is not None]
        measurement_ids = sorted({int(item["measurement_id"]) for item in details})
        row = {
            "queue_id": _stable_id(paper_id, family, REVIEW_RULE_VERSION),
            **{key: primary.get(key) for key in (
                "paper_id", "article_number", "doi", "title", "authors", "year",
                "venue", "citation_count", "pdf_available", "ieee_url",
                "link_medium", "link_subtype", "energy_component_scope",
            )},
            "priority": priorities[0],
            "flag_family": family,
            "flag_codes": sorted({item["flag_code"] for item in details}),
            "flag_count": len(details),
            "measurement_count": len(measurement_ids),
            "measurement_ids": measurement_ids,
            "actual_value": actual_values[0] if len(actual_values) == 1 else actual_values,
            "expected_value": expected_values[0] if len(expected_values) == 1 else expected_values,
            "delta": max(deltas, key=abs) if deltas else None,
            "group_median": median_values[0] if median_values else None,
            "robust_z": max(robust_values, key=abs) if robust_values else None,
            "recommended_action": " | ".join(dict.fromkeys(item["recommended_action"] for item in details)),
            "review_status": "pending",
            "reviewer_value": None,
            "reviewer_scope": None,
            "reviewer_note": None,
            "rule_version": REVIEW_RULE_VERSION,
        }
        rows.append(row)
    rows.sort(key=lambda row: (
        PRIORITY_ORDER.get(row["priority"], 9), row.get("venue") or "",
        -(int(row.get("year")) if str(row.get("year") or "").isdigit() else 0),
        int(row["paper_id"]), row["flag_family"],
    ))
    for index, row in enumerate(rows, 1):
        row["sequence"] = index
        row["batch_no"] = (index - 1) // batch_size + 1
        row["batch_item"] = (index - 1) % batch_size + 1
    return rows


def build_review_package(
    measurements: Iterable[dict],
    evidence_rows: Iterable[dict],
    *,
    batch_size: int = 50,
    generated_at: str | None = None,
) -> dict:
    """Return Review Queue, detail flags, and workbook-support metadata."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    evidence_by_measurement = defaultdict(list)
    for raw in evidence_rows:
        item = {key: json_value(value) for key, value in raw.items()}
        measurement_id = int(item.get("measurement_id"))
        evidence_by_measurement[measurement_id].append(item)
    for values in evidence_by_measurement.values():
        values.sort(key=lambda item: int(item.get("evidence_id") or item.get("id") or 0))

    enriched = []
    for raw in measurements:
        measurement_id = int(raw.get("measurement_id") or raw.get("id"))
        enriched.append(_enrich_measurement(dict(raw), evidence_by_measurement.get(measurement_id, [])))

    flags = []
    for row in enriched:
        flags.extend(_row_flags(row, evidence_by_measurement.get(row["measurement_id"], [])))
    flags.extend(_robust_flags(enriched))
    flags.sort(key=lambda item: (
        PRIORITY_ORDER.get(item["priority"], 9), int(item["paper_id"]),
        item["flag_family"], item["flag_code"], int(item["measurement_id"]),
    ))
    queue = _queue_rows(flags, batch_size)
    priority_counts = Counter(row["priority"] for row in queue)
    queue_family_counts = Counter(row["flag_family"] for row in queue)
    detail_family_counts = Counter(row["flag_family"] for row in flags)
    flagged_measurements = {int(row["measurement_id"]) for row in flags}
    flagged_papers = {int(row["paper_id"]) for row in flags}
    energy_rows = [row for row in enriched if number(row.get("energy_pj_bit")) is not None]
    scope_unknown_rows = [
        row for row in energy_rows if row.get("energy_component_scope") == "unknown"
    ]
    scope_unknown_reasons = Counter(
        reason
        for row in scope_unknown_rows
        for reason in (row.get("scope_reason_codes") or ["unspecified"])
    )
    summary = {
        "generated_at": generated_at or datetime.now().isoformat(timespec="seconds"),
        "rule_version": REVIEW_RULE_VERSION,
        "measurement_population": len(enriched),
        "paper_population": len({int(row["paper_id"]) for row in enriched}),
        "flagged_measurements": len(flagged_measurements),
        "flagged_papers": len(flagged_papers),
        "measurement_flag_rows": len(flags),
        "queue_rows": len(queue),
        "batch_size": batch_size,
        "batch_count": max((row["batch_no"] for row in queue), default=0),
        "energy_measurements": len(energy_rows),
        "energy_scope_unknown_measurements": len(scope_unknown_rows),
        "energy_scope_unknown_by_reason": dict(sorted(scope_unknown_reasons.items())),
        "queue_by_priority": dict(sorted(priority_counts.items())),
        "queue_by_family": dict(sorted(queue_family_counts.items())),
        "detail_by_family": dict(sorted(detail_family_counts.items())),
    }
    data_dictionary = [
        {"field": field, "section": section, "description": description}
        for field, section, description in DATA_DICTIONARY
    ]
    return {
        "summary": summary,
        "review_queue": queue,
        "measurement_flags": flags,
        "rules": [dict(item, rule_version=REVIEW_RULE_VERSION) for item in RULES],
        "data_dictionary": data_dictionary,
        "review_status_values": sorted(VALID_REVIEW_STATUSES),
    }
