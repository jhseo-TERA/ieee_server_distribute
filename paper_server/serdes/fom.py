# -*- coding: utf-8 -*-
"""Rebuild SerDes energy/bit with the survey spreadsheet's P/R identity.

The dimensional identity is exact for the stored units::

    Power [mW] / Data rate [Gb/s] = Energy [pJ/bit]

This module deliberately separates *reported* energy from a calculated value.
It only fills an empty energy field when power and rate belong to the same
measurement and have compatible field-level evidence.  Existing reported
energy is never overwritten; it is only cross-checked.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
import hashlib
import json
import math
import re
from typing import Any, Iterable


FOM_EXTRACTOR_VERSION = "serdes-fom-pr-1.0"
FOM_ENERGY_BASIS = "calculated_power_per_rate"
FOM_ENERGY_SCOPE = "calculated_same_point"
FOM_EVIDENCE_SOURCE = "derived_fom"
FOM_DERIVATION_METHOD = "dimensional_identity_power_over_rate"
FOM_CROSSCHECK_METHOD = "dimensional_identity_crosscheck"

CURATED_ROW_SOURCES = frozenset({"user_sheet", "reference_xlsx"})
REJECTED_REVIEW_STATUSES = frozenset({"rejected", "invalid"})
NON_APPLY_REVIEW_STATUSES = frozenset({"rejected", "invalid", "needs_review"})
RATE_EVIDENCE_FIELDS = {
    "reported_rate_gbps": (
        "reported_rate_gbps", "lane_rate_gbps", "aggregate_rate_gbps",
    ),
    "lane_rate_gbps": ("lane_rate_gbps", "reported_rate_gbps"),
    "aggregate_rate_gbps": (
        "aggregate_rate_gbps", "reported_rate_gbps", "lane_rate_gbps",
    ),
}

LATEST_INCLUDED_MEASUREMENTS_SQL = """
SELECT m.*, i.canonical_paper_id paper_id, p.article_number,
       COALESCE(p.title, m.reference_title, i.canonical_title) paper_title,
       COALESCE(p.year, m.publication_year, i.canonical_year) paper_year,
       COALESCE(p.source_name, m.publication_name, i.canonical_venue) paper_venue,
       p.url paper_url
FROM serdes_measurements m
JOIN serdes_implementations i ON i.id=m.implementation_id
LEFT JOIN papers p ON p.id=i.canonical_paper_id
WHERE (
    m.power_mw IS NOT NULL
    OR m.energy_basis=%s
)
AND m.review_status NOT IN ('rejected','invalid')
AND EXISTS (
    SELECT 1
    FROM serdes_paper_screenings s
    WHERE s.paper_id=i.canonical_paper_id
      AND s.include_in_survey=1
      AND s.run_id IN (
          SELECT MAX(r.id)
          FROM serdes_screening_runs r
          WHERE r.status='complete' AND r.scope_name LIKE 'all%%'
          GROUP BY r.venue
      )
)
ORDER BY m.id
"""


@dataclass(frozen=True)
class RateChoice:
    eligible: bool
    field: str | None = None
    value: float | None = None
    reason: str = ""
    power_scope: str = "unknown"


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, InvalidOperation):
        return None
    return number if math.isfinite(number) and number > 0 else None


def calculate_energy_pj_bit(power_mw: Any, rate_gbps: Any) -> float | None:
    """Return P/R in pJ/bit, or ``None`` for non-positive operands."""
    power = _number(power_mw)
    rate = _number(rate_gbps)
    if power is None or rate is None:
        return None
    return float(Decimal(str(power)) / Decimal(str(rate)))


def fom_consistency(
    reported_energy: Any,
    calculated_energy: Any,
    *,
    absolute_tolerance: float = 0.01,
    relative_tolerance: float = 0.02,
) -> dict[str, Any]:
    """Cross-check a reported energy value without changing it."""
    reported = _number(reported_energy)
    calculated = _number(calculated_energy)
    if reported is None or calculated is None:
        return {"status": "not_checkable"}
    delta = abs(reported - calculated)
    tolerance = max(absolute_tolerance, relative_tolerance * abs(reported))
    return {
        "status": "match" if delta <= tolerance else "conflict",
        "absolute_delta": delta,
        "relative_delta": delta / max(abs(reported), 1e-12),
        "tolerance": tolerance,
    }


def _normalize_context(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _longest_overlap(left: str, right: str) -> int:
    if not left or not right:
        return 0
    return SequenceMatcher(None, left, right, autojunk=False).find_longest_match(
        0, len(left), 0, len(right)
    ).size


def _metric_bound(text: str, unit_pattern: str) -> bool:
    number = r"[-+]?(?:\d+(?:\.\d+)?|\.\d+)"
    prefix = (
        r"(?:<|>|≤|≥|sub[- ]?|under\s+|below\s+|above\s+|up\s+to\s+|"
        r"less\s+than\s+|more\s+than\s+|at\s+most\s+|at\s+least\s+)"
    )
    return bool(re.search(
        rf"{prefix}\s*(?:about|approximately|approx\.?|~)?\s*{number}\s*{unit_pattern}",
        text,
        re.IGNORECASE,
    ))


def power_semantic_guard(evidence_text: Any) -> str | None:
    """Reject mW values that are not exact electrical consumption."""
    text = _normalize_context(evidence_text)
    if _metric_bound(text, r"(?:[µμu]?w|mw)\b"):
        return "bounded_power"
    if re.search(
        r"\b(?:[µμu]?w|mw)\s*/\s*(?:g(?:b|bps|bit(?:/s)?)|gb/s)\b",
        text,
        re.IGNORECASE,
    ):
        return "already_normalized_power_per_rate"
    if re.search(r"\b(?:off[- ]?state|standby|sleep[- ]mode|idle[- ]mode)\b", text):
        return "inactive_state_power"
    if re.search(
        r"(?:\b(?:[µμu]?w|mw)\s*/\s*(?:ch(?:annel)?|lane)\b|"
        r"\b(?:[µμu]?w|mw)\b.{0,25}\bper\s+channel\b)",
        text,
        re.IGNORECASE,
    ):
        return "per_channel_power_scope"
    optical_output = re.compile(
        r"(?:\boma\b|optical\s+modulation\s+amplitude|optical\s+(?:output\s+)?power|"
        r"laser\s+(?:output\s+)?power|\bpout\b|radiated\s+power|eirp)",
        re.IGNORECASE,
    )
    if optical_output.search(text):
        return "non_consumption_power"
    sub_block = re.compile(
        r"(?:wavelength[- ]stabili[sz]ation|thermal\s+tuning|heater\s+power|"
        r"auxiliary\s+power|front[- ]end\s+only|equalizer\s+only|"
        r"clock(?:ing)?\s+only|excluding\s+(?:the\s+)?(?:cdr|pll|driver|tia|frontend|front[- ]end))",
        re.IGNORECASE,
    )
    if sub_block.search(text):
        return "subblock_power"
    return None


def rate_semantic_guard(evidence_text: Any) -> str | None:
    text = _normalize_context(evidence_text)
    if _metric_bound(text, r"(?:[tg]b\s*/\s*s|[tg]bps|[tg]bit\s*/\s*s)\b"):
        return "bounded_rate"
    number = r"(?:\d+(?:\.\d+)?|\.\d+)"
    unit = r"(?:[tg]b\s*/\s*s|[tg]bps|[tg]bit\s*/\s*s)\b"
    if re.search(
        rf"{number}\s*(?:(?:-|–)\s*to\s*(?:-|–)|to|[-–])\s*{number}\s*{unit}",
        text,
        re.IGNORECASE,
    ):
        return "rate_range_without_operating_point"
    return None


def infer_power_scope(evidence_text: Any, stored_scope: Any = None) -> str:
    stored = _normalize_context(stored_scope).replace("-", "_").replace(" ", "_")
    if stored in {"lane", "per_lane", "each_lane"}:
        return "lane"
    if stored in {"aggregate", "total", "system", "link", "full_link", "chip"}:
        return "aggregate"
    text = _normalize_context(evidence_text)
    if re.search(r"\b(?:per|each)\s+lane\b|\blane[- ]power\b", text):
        return "lane"
    if re.search(
        r"\b(?:total|aggregate|overall|entire|full[- ]link)\b|"
        r"\b(?:transceiver|receiver|transmitter|chip|design)\s+(?:core\s+)?(?:consumes?|dissipates?)\b",
        text,
    ):
        return "aggregate"
    return "unknown"


def select_rate_denominator(row: dict[str, Any], power_scope: str = "unknown") -> RateChoice:
    """Choose a bit-rate denominator without silently mixing lane and total scope."""
    reported = _number(row.get("reported_rate_gbps"))
    lane = _number(row.get("lane_rate_gbps"))
    aggregate = _number(row.get("aggregate_rate_gbps"))
    lane_count = _number(row.get("lane_count"))
    rate_scope = _normalize_context(row.get("rate_scope")) or "unknown"
    source_kind = _normalize_context(row.get("source_kind"))

    # The reference workbooks define their Speed column as the denominator.
    if source_kind in CURATED_ROW_SOURCES:
        for field, value in (
            ("reported_rate_gbps", reported),
            ("lane_rate_gbps", lane),
            ("aggregate_rate_gbps", aggregate),
        ):
            if value is not None:
                return RateChoice(True, field, value, "spreadsheet_speed_column", power_scope)
        return RateChoice(False, reason="missing_fixed_bit_rate", power_scope=power_scope)

    if (
        _number(row.get("reported_rate_min_gbps")) is not None
        or _number(row.get("reported_rate_max_gbps")) is not None
    ):
        return RateChoice(
            False, reason="rate_range_without_operating_point", power_scope=power_scope,
        )

    if rate_scope == "aggregate":
        value = aggregate or reported
        field = "aggregate_rate_gbps" if aggregate is not None else "reported_rate_gbps"
        return (
            RateChoice(True, field, value, "explicit_aggregate_rate", power_scope)
            if value is not None
            else RateChoice(False, reason="missing_aggregate_rate", power_scope=power_scope)
        )

    is_multilane = lane_count is not None and lane_count > 1
    if is_multilane:
        if power_scope == "lane":
            value = lane or reported
            field = "lane_rate_gbps" if lane is not None else "reported_rate_gbps"
            return (
                RateChoice(True, field, value, "per_lane_power", power_scope)
                if value is not None
                else RateChoice(False, reason="missing_lane_rate", power_scope=power_scope)
            )
        if power_scope == "aggregate":
            return (
                RateChoice(True, "aggregate_rate_gbps", aggregate, "total_power_multilane", power_scope)
                if aggregate is not None
                else RateChoice(False, reason="missing_aggregate_rate", power_scope=power_scope)
            )
        return RateChoice(False, reason="multilane_power_scope_unknown", power_scope=power_scope)

    if power_scope == "lane" or rate_scope == "lane":
        value = lane or reported
        field = "lane_rate_gbps" if lane is not None else "reported_rate_gbps"
        return (
            RateChoice(True, field, value, "single_lane_rate", power_scope)
            if value is not None
            else RateChoice(False, reason="missing_lane_rate", power_scope=power_scope)
        )

    if power_scope == "aggregate" and aggregate is not None:
        return RateChoice(True, "aggregate_rate_gbps", aggregate, "explicit_total_power", power_scope)
    if reported is not None:
        return RateChoice(True, "reported_rate_gbps", reported, "single_reported_rate", power_scope)
    if lane is not None:
        return RateChoice(True, "lane_rate_gbps", lane, "single_lane_rate", power_scope)
    if aggregate is not None:
        return RateChoice(True, "aggregate_rate_gbps", aggregate, "only_aggregate_rate", power_scope)
    return RateChoice(False, reason="missing_fixed_bit_rate", power_scope=power_scope)


def _evidence_candidates(
    evidence_rows: Iterable[dict[str, Any]],
    fields: Iterable[str],
    source_kind: str,
) -> list[dict[str, Any]]:
    accepted_fields = set(fields)
    return [
        item for item in evidence_rows
        if item.get("field_name") in accepted_fields
        and item.get("source_kind") == source_kind
        and item.get("review_status") not in REJECTED_REVIEW_STATUSES | {"needs_review"}
    ]


def _same_locator(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if (left.get("source_locator") or "") != (right.get("source_locator") or ""):
        return False
    left_abstract = left.get("abstract_id")
    right_abstract = right.get("abstract_id")
    if left_abstract is not None or right_abstract is not None:
        return left_abstract is not None and left_abstract == right_abstract
    return True


def compatible_input_evidence(
    row: dict[str, Any],
    evidence_rows: Iterable[dict[str, Any]],
    rate_field: str,
    *,
    min_abstract_overlap: int = 40,
) -> dict[str, Any]:
    """Find a same-source, same-locator power/rate evidence pair."""
    source_kind = str(row.get("source_kind") or "")
    powers = _evidence_candidates(evidence_rows, ("power_mw",), source_kind)
    rates = _evidence_candidates(
        evidence_rows, RATE_EVIDENCE_FIELDS.get(rate_field, (rate_field,)), source_kind,
    )
    if not powers:
        return {"eligible": False, "reason": "missing_power_evidence"}
    if not rates:
        return {"eligible": False, "reason": "missing_rate_evidence"}

    pairs = []
    for power in powers:
        power_text = _normalize_context(power.get("evidence_text"))
        for rate in rates:
            if not _same_locator(power, rate):
                continue
            rate_text = _normalize_context(rate.get("evidence_text"))
            overlap = _longest_overlap(power_text, rate_text)
            pairs.append((overlap, float(power.get("confidence") or 0), power, rate))
    if not pairs:
        return {"eligible": False, "reason": "cross_source_or_locator"}
    overlap, _, power, rate = max(pairs, key=lambda item: (item[0], item[1]))
    if source_kind == "abstract" and overlap < min_abstract_overlap:
        return {
            "eligible": False,
            "reason": "abstract_context_not_shared",
            "context_overlap_chars": overlap,
            "power_evidence_id": power.get("id"),
            "rate_evidence_id": rate.get("id"),
        }
    return {
        "eligible": True,
        "reason": "same_source_locator",
        "context_overlap_chars": overlap,
        "power_evidence_id": power.get("id"),
        "rate_evidence_id": rate.get("id"),
        "source_locator": power.get("source_locator"),
        "source_url": power.get("source_url") or rate.get("source_url"),
        "abstract_id": power.get("abstract_id") or rate.get("abstract_id"),
        "paper_id": power.get("paper_id") or rate.get("paper_id") or row.get("paper_id"),
        "power_evidence_text": power.get("evidence_text") or "",
        "rate_evidence_text": rate.get("evidence_text") or "",
        "power_confidence": float(power.get("confidence") or 0),
        "rate_confidence": float(rate.get("confidence") or 0),
    }


def _base_result(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "measurement_id": int(row["id"]),
        "implementation_id": int(row["implementation_id"]),
        "operating_point_key": row.get("operating_point_key"),
        "paper_id": row.get("paper_id"),
        "article_number": row.get("article_number"),
        "title": row.get("paper_title") or row.get("reference_title"),
        "year": row.get("paper_year") or row.get("publication_year"),
        "venue": row.get("paper_venue") or row.get("publication_name"),
        "url": row.get("paper_url"),
        "source_kind": row.get("source_kind"),
        "review_status": row.get("review_status"),
        "existing_energy_pj_bit": _number(row.get("energy_pj_bit")),
        "existing_energy_basis": row.get("energy_basis"),
        "power_mw": _number(row.get("power_mw")),
        "rate_scope": row.get("rate_scope"),
        "power_scope": row.get("power_scope"),
        "lane_count": _number(row.get("lane_count")),
    }


def evaluate_measurement(
    row: dict[str, Any],
    evidence_rows: Iterable[dict[str, Any]],
    *,
    min_abstract_overlap: int = 40,
    min_energy_pj_bit: float = 0.001,
    max_energy_pj_bit: float = 1000.0,
) -> dict[str, Any]:
    """Return a deterministic derive/audit/skip decision for one row."""
    result = _base_result(row)
    evidence_rows = list(evidence_rows)
    existing = result["existing_energy_pj_bit"]
    is_owned_calculation = row.get("energy_basis") == FOM_ENERGY_BASIS
    power = result["power_mw"]
    if power is None:
        result.update(action="clear_stale" if is_owned_calculation else "skip", reason="missing_power")
        return result

    source_kind = str(row.get("source_kind") or "")
    power_candidates = _evidence_candidates(evidence_rows, ("power_mw",), source_kind)
    if not power_candidates:
        result.update(action="clear_stale" if is_owned_calculation else "skip", reason="missing_power_evidence")
        return result
    best_power = max(
        power_candidates,
        key=lambda item: (float(item.get("confidence") or 0), int(item.get("id") or 0)),
    )
    if source_kind not in CURATED_ROW_SOURCES:
        guard = power_semantic_guard(best_power.get("evidence_text"))
        if guard:
            result.update(action="clear_stale" if is_owned_calculation else "skip", reason=guard)
            return result
    inferred_scope = infer_power_scope(best_power.get("evidence_text"), row.get("power_scope"))
    choice = select_rate_denominator(row, inferred_scope)
    result.update(
        selected_rate_field=choice.field,
        selected_rate_gbps=choice.value,
        denominator_reason=choice.reason,
        inferred_power_scope=choice.power_scope,
    )
    if not choice.eligible:
        result.update(action="clear_stale" if is_owned_calculation else "skip", reason=choice.reason)
        return result

    provenance = compatible_input_evidence(
        row, evidence_rows, choice.field or "reported_rate_gbps",
        min_abstract_overlap=min_abstract_overlap,
    )
    result.update({key: value for key, value in provenance.items() if key != "eligible"})
    if not provenance.get("eligible"):
        result.update(
            action="clear_stale" if is_owned_calculation else "skip",
            reason=provenance.get("reason") or "incompatible_evidence",
        )
        return result
    if source_kind not in CURATED_ROW_SOURCES:
        rate_guard = rate_semantic_guard(provenance.get("rate_evidence_text"))
        if rate_guard:
            result.update(action="clear_stale" if is_owned_calculation else "skip", reason=rate_guard)
            return result

    calculated = calculate_energy_pj_bit(power, choice.value)
    result["calculated_energy_pj_bit"] = calculated
    if calculated is None or not min_energy_pj_bit <= calculated <= max_energy_pj_bit:
        result.update(
            action="clear_stale" if is_owned_calculation else "skip",
            reason="calculated_energy_outlier",
        )
        return result

    source_caps = {
        "user_sheet": 0.98,
        "reference_xlsx": 0.94,
        "abstract": 0.76,
        "title": 0.64,
        "pdf": 0.90,
        "manual": 0.99,
    }
    confidence = min(
        float(row.get("overall_confidence") or 0.7),
        float(provenance.get("power_confidence") or 1),
        float(provenance.get("rate_confidence") or 1),
        source_caps.get(source_kind, 0.70),
    )
    result["calculation_confidence"] = max(0.0, min(1.0, confidence))

    if existing is not None and not is_owned_calculation:
        check = fom_consistency(existing, calculated)
        result.update(check)
        result["action"] = "audit_match" if check["status"] == "match" else "audit_conflict"
        result["reason"] = f"reported_energy_{check['status']}"
        return result
    if row.get("review_status") in NON_APPLY_REVIEW_STATUSES:
        result.update(action="review_only", reason="measurement_not_auto_applicable")
        return result
    result.update(
        action="refresh_derived" if is_owned_calculation else "derive",
        reason="same_operating_point_power_over_rate",
    )
    return result


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _evidence_text(decision: dict[str, Any], crosscheck: bool = False) -> str:
    calculated = decision.get("calculated_energy_pj_bit")
    base = (
        f"Energy = {decision.get('power_mw'):.12g} mW / "
        f"{decision.get('selected_rate_gbps'):.12g} Gb/s = {calculated:.12g} pJ/bit; "
        f"measurement_id={decision['measurement_id']}; "
        f"operating_point_key={decision.get('operating_point_key')}; "
        f"denominator={decision.get('selected_rate_field')}; "
        f"denominator_reason={decision.get('denominator_reason')}; "
        f"power_evidence_id={decision.get('power_evidence_id')}; "
        f"rate_evidence_id={decision.get('rate_evidence_id')}; "
        f"source={decision.get('source_kind')}; locator={decision.get('source_locator')}"
    )
    if crosscheck:
        base += (
            f"; reported={decision.get('existing_energy_pj_bit'):.12g} pJ/bit; "
            f"crosscheck={decision.get('status')}; "
            f"absolute_delta={decision.get('absolute_delta'):.12g} pJ/bit"
        )
    return base


def _upsert_fom_evidence(cur, decision: dict[str, Any], *, crosscheck: bool) -> None:
    field_name = "energy_pj_bit_crosscheck" if crosscheck else "energy_pj_bit"
    method = FOM_CROSSCHECK_METHOD if crosscheck else FOM_DERIVATION_METHOD
    evidence_text = _evidence_text(decision, crosscheck=crosscheck)
    evidence_sha = _sha256(evidence_text)
    evidence_key = _sha256(
        "|".join((
            str(decision["measurement_id"]), field_name,
            FOM_EVIDENCE_SOURCE, evidence_sha,
        ))
    )
    input_record = json.dumps({
        "measurement_id": decision["measurement_id"],
        "power_mw": decision.get("power_mw"),
        "rate_field": decision.get("selected_rate_field"),
        "rate_gbps": decision.get("selected_rate_gbps"),
        "power_evidence_id": decision.get("power_evidence_id"),
        "rate_evidence_id": decision.get("rate_evidence_id"),
    }, sort_keys=True)
    status = (
        "needs_review" if decision.get("action") == "audit_conflict"
        else ("verified" if crosscheck else "calculated")
    )
    cur.execute(
        """
        INSERT INTO serdes_measurement_evidence
            (evidence_key, measurement_id, field_name, paper_id, abstract_id,
             source_kind, source_url, source_locator, evidence_text,
             source_sha256, evidence_sha256, extraction_method,
             extractor_version, confidence, review_status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            source_url=VALUES(source_url), source_locator=VALUES(source_locator),
            confidence=VALUES(confidence), review_status=VALUES(review_status),
            abstract_id=VALUES(abstract_id), evidence_text=VALUES(evidence_text),
            updated_at=CURRENT_TIMESTAMP
        """,
        (
            evidence_key, decision["measurement_id"], field_name,
            decision.get("paper_id"), decision.get("abstract_id"),
            FOM_EVIDENCE_SOURCE, decision.get("source_url") or decision.get("url"),
            decision.get("source_locator"), evidence_text, _sha256(input_record),
            evidence_sha, method, FOM_EXTRACTOR_VERSION,
            decision.get("calculation_confidence") or 0.6, status,
        ),
    )


def fetch_fom_rows(conn) -> tuple[list[dict[str, Any]], dict[int, list[dict[str, Any]]]]:
    with conn.cursor() as cur:
        cur.execute(LATEST_INCLUDED_MEASUREMENTS_SQL, (FOM_ENERGY_BASIS,))
        rows = list(cur.fetchall())
        measurement_ids = [int(row["id"]) for row in rows]
        evidence: dict[int, list[dict[str, Any]]] = defaultdict(list)
        if measurement_ids:
            placeholders = ",".join(["%s"] * len(measurement_ids))
            cur.execute(
                f"""
                SELECT id, measurement_id, field_name, paper_id, abstract_id,
                       source_kind, source_url, source_locator, evidence_text,
                       confidence, review_status, extraction_method, extractor_version
                FROM serdes_measurement_evidence
                WHERE measurement_id IN ({placeholders})
                  AND field_name IN (
                      'power_mw','reported_rate_gbps','lane_rate_gbps',
                      'aggregate_rate_gbps'
                  )
                ORDER BY id
                """,
                tuple(measurement_ids),
            )
            for item in cur.fetchall():
                evidence[int(item["measurement_id"])].append(item)
    return rows, evidence


def regenerate_fom(
    conn,
    *,
    apply: bool = False,
    min_abstract_overlap: int = 40,
    min_energy_pj_bit: float = 0.001,
    max_energy_pj_bit: float = 1000.0,
) -> dict[str, Any]:
    """Audit and optionally apply spreadsheet-style FoM regeneration."""
    rows, evidence = fetch_fom_rows(conn)
    decisions = [
        evaluate_measurement(
            row, evidence.get(int(row["id"]), ()),
            min_abstract_overlap=min_abstract_overlap,
            min_energy_pj_bit=min_energy_pj_bit,
            max_energy_pj_bit=max_energy_pj_bit,
        )
        for row in rows
    ]
    action_counts = Counter(item["action"] for item in decisions)
    reason_counts = Counter(item["reason"] for item in decisions)
    source_actions: dict[str, Counter] = defaultdict(Counter)
    for item in decisions:
        source_actions[str(item.get("source_kind") or "unknown")][item["action"]] += 1

    changed_measurements = 0
    evidence_written = 0
    with conn.cursor() as cur:
        if apply:
            # This script owns only its derived/cross-check evidence and can
            # therefore rebuild that evidence idempotently.
            cur.execute(
                "DELETE FROM serdes_measurement_evidence "
                "WHERE source_kind=%s AND extractor_version=%s",
                (FOM_EVIDENCE_SOURCE, FOM_EXTRACTOR_VERSION),
            )
            for decision in decisions:
                action = decision["action"]
                if action in {"derive", "refresh_derived"}:
                    cur.execute(
                        """
                        UPDATE serdes_measurements
                        SET energy_pj_bit=%s, energy_scope=%s, energy_basis=%s
                        WHERE id=%s
                          AND (energy_pj_bit IS NULL OR energy_basis=%s)
                        """,
                        (
                            decision["calculated_energy_pj_bit"], FOM_ENERGY_SCOPE,
                            FOM_ENERGY_BASIS, decision["measurement_id"], FOM_ENERGY_BASIS,
                        ),
                    )
                    changed_measurements += int(cur.rowcount > 0)
                    _upsert_fom_evidence(cur, decision, crosscheck=False)
                    evidence_written += 1
                elif action == "clear_stale":
                    cur.execute(
                        """
                        UPDATE serdes_measurements
                        SET energy_pj_bit=NULL, energy_scope='unknown', energy_basis=NULL
                        WHERE id=%s AND energy_basis=%s
                        """,
                        (decision["measurement_id"], FOM_ENERGY_BASIS),
                    )
                    changed_measurements += int(cur.rowcount > 0)
                elif action in {"audit_match", "audit_conflict"}:
                    _upsert_fom_evidence(cur, decision, crosscheck=True)
                    evidence_written += 1
            conn.commit()
        else:
            conn.rollback()

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) measurement_total,
                   COUNT(DISTINCT i.canonical_paper_id) paper_total
            FROM serdes_measurements m
            JOIN serdes_implementations i ON i.id=m.implementation_id
            WHERE m.energy_basis=%s
              AND EXISTS (
                  SELECT 1 FROM serdes_paper_screenings s
                  WHERE s.paper_id=i.canonical_paper_id
                    AND s.include_in_survey=1
                    AND s.run_id IN (
                        SELECT MAX(r.id) FROM serdes_screening_runs r
                        WHERE r.status='complete' AND r.scope_name LIKE 'all%%'
                        GROUP BY r.venue
                    )
              )
            """,
            (FOM_ENERGY_BASIS,),
        )
        current_fom = cur.fetchone() or {}
        cur.execute(
            """
            SELECT COUNT(*) evidence_total,
                   SUM(review_status='needs_review') conflict_total,
                   COUNT(DISTINCT measurement_id) measurement_total
            FROM serdes_measurement_evidence
            WHERE source_kind=%s AND extractor_version=%s
            """,
            (FOM_EVIDENCE_SOURCE, FOM_EXTRACTOR_VERSION),
        )
        current_evidence = cur.fetchone() or {}

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "applied": bool(apply),
        "extractor_version": FOM_EXTRACTOR_VERSION,
        "formula": "Power [mW] / Speed [Gb/s] = Energy [pJ/bit]",
        "policy": {
            "population": "latest completed Core + Adjacent screening runs",
            "same_measurement_only": True,
            "same_source_locator_required": True,
            "abstract_min_shared_context_chars": min_abstract_overlap,
            "existing_reported_energy_overwritten": False,
            "reported_crosscheck_tolerance": "max(0.01 pJ/bit, 2%)",
            "calculated_energy_bounds_pj_bit": [min_energy_pj_bit, max_energy_pj_bit],
            "energy_basis": FOM_ENERGY_BASIS,
        },
        "summary": {
            "measurements_evaluated": len(decisions),
            "changed_measurements": changed_measurements,
            "evidence_written": evidence_written,
            "calculated_measurements_current": int(
                current_fom.get("measurement_total") or 0
            ),
            "calculated_papers_current": int(current_fom.get("paper_total") or 0),
            "generated_evidence_current": int(
                current_evidence.get("evidence_total") or 0
            ),
            "crosscheck_conflicts_current": int(
                current_evidence.get("conflict_total") or 0
            ),
            "actions": dict(sorted(action_counts.items())),
            "reasons": dict(sorted(reason_counts.items())),
            "source_actions": {
                source: dict(sorted(counts.items()))
                for source, counts in sorted(source_actions.items())
            },
        },
        "decisions": decisions,
    }


def json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "__dict__"):
        return asdict(value)
    return str(value)
