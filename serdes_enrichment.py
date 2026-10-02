"""Auditable SerDes survey enrichment beyond raw performance extraction.

The helpers in this module keep comparison dimensions separate:

* physical link medium/subtype is a paper-level property;
* energy component scope is an operating-point/measurement property;
* evidence tier describes provenance, not circuit quality;
* completeness reports which fields exist without inventing missing values.
"""

from __future__ import annotations

from collections import defaultdict
import math
import re
from typing import Any, Callable, Iterable

from serdes_metrics import plain_text


ENERGY_COMPONENT_SCOPE_VERSION = "serdes-energy-scope-1.1"
LINK_SUBTYPE_VERSION = "serdes-link-subtype-1.1"

VALID_ENERGY_COMPONENT_SCOPES = (
    "tx", "rx", "trx", "full_link", "driver_only", "unknown",
)
VALID_LINK_SUBTYPES = (
    "vcsel", "silicon_photonic", "eml_dml", "pon", "optical_other",
    "die_to_die", "memory_io", "backplane", "cable", "chip_to_chip",
    "electrical_other", "mixed_or_unknown",
)

ENERGY_OR_POWER_RE = re.compile(
    r"(?:\b(?:energy(?: efficiency)?|power(?: consumption)?)\b|"
    r"\d+(?:\.\d+)?\s*(?:[pf]j\s*/\s*(?:b|bit)|[µμu]?w|mw))",
    re.IGNORECASE,
)
FULL_LINK_SCOPE_RE = re.compile(
    r"\b(?:full[- ]link|complete[- ]link|entire[- ]link|end[- ]to[- ]end|"
    r"whole[- ]link|link[- ]level|total link|both (?:the )?transmitter and (?:the )?receiver|"
    r"transmitter\s*\+\s*receiver)\b",
    re.IGNORECASE,
)
DRIVER_ONLY_SCOPE_RE = re.compile(
    r"\b(?:(?:vcsel|laser|modulator|mzm|eml|dml|output|line)\s+drivers?|"
    r"driver\s+(?:ics?|circuits?|amplifiers?))\b",
    re.IGNORECASE,
)
TRX_SCOPE_RE = re.compile(
    r"\b(?:transceivers?|tx\s*[/+&-]\s*rx|rx\s*[/+&-]\s*tx|"
    r"transmitters?\s+(?:and|&)\s+receivers?)\b",
    re.IGNORECASE,
)
TX_SCOPE_RE = re.compile(
    r"\b(?:transmitters?|serializers?|tx|sst\s+drivers?|source[- ]series[- ]terminated\s+drivers?)\b",
    re.IGNORECASE,
)
TX_PRIMARY_SCOPE_RE = re.compile(r"\b(?:transmitters?|serializers?|tx)\b", re.I)
RX_PRIMARY_SCOPE_RE = re.compile(
    r"\b(?:receivers?|deserializers?|rx|trans[- ]?impedance amplifiers?|tia)\b",
    re.I,
)
RX_SCOPE_RE = re.compile(
    r"\b(?:receivers?|deserializers?|rx|clock(?:\s+and)?\s+data recovery|"
    r"clock recovery|cdr|trans[- ]?impedance amplifiers?|tia)\b",
    re.IGNORECASE,
)
TRX_SYSTEM_HEAD_RE = re.compile(r"\b(?:serdes|transceivers?)\b", re.I)
FULL_LINK_HEAD_RE = re.compile(
    r"\b(?:(?:serial|wireline|electrical|optical|bidirectional)\s+links?|"
    r"chip[- ]to[- ]chip\s+links?|die[- ]to[- ]die\s+links?)\b",
    re.I,
)
PARTIAL_SCOPE_RE = re.compile(
    r"\b(?:analog[- ]only|analog\s+(?:power|energy)(?:\s+(?:only|portion))?|"
    r"front[- ]end\s+only|"
    r"excluding\s+(?:the\s+)?(?:dsp|cdr|clock|driver|laser|modulator)|"
    r"does\s+not\s+include|without\s+(?:the\s+)?(?:dsp|cdr|driver)|"
    r"partial\s+(?:power|energy))\b",
    re.IGNORECASE,
)

OPTICAL_SUBTYPE_PATTERNS = (
    ("vcsel", re.compile(r"\b(?:vcsel|vertical[- ]cavity surface[- ]emitting laser)\b", re.I)),
    (
        "silicon_photonic",
        re.compile(
            r"\b(?:silicon[- ]photon\w*|si[- ]photon\w*|sipho?n|"
            r"silicon\s+(?:microrings?|micro[- ]rings?|modulators?|photodiodes?))\b",
            re.I,
        ),
    ),
    (
        "eml_dml",
        re.compile(
            r"\b(?:eml|dml|electro[- ]absorption modulated lasers?|"
            r"directly[- ]modulated lasers?|electro[- ]absorption modulators?)\b",
            re.I,
        ),
    ),
    (
        "pon",
        re.compile(r"\b(?:(?:e|g|xg|xgs|10g|25g|50g)?-?pon|gpon|epon)\b", re.I),
    ),
)

ELECTRICAL_SUBTYPE_PATTERNS = (
    (
        "memory_io",
        re.compile(
            r"\b(?:ddr\d*|lpddr\d*|gddr\d*|hbm(?:2e?|3e?|4)|"
            r"high[- ]bandwidth\s+memory|memory\s+(?:i/?o|interfaces?))\b",
            re.I,
        ),
    ),
    (
        "die_to_die",
        re.compile(
            r"\b(?:die[- ]to[- ]die|d2d\s+(?:links?|interfaces?|interconnects?)|"
            r"chiplets?|ucie|aib(?:[- ]compatible)?|nvlink[- ]c2c)\b",
            re.I,
        ),
    ),
    ("backplane", re.compile(r"\bbackplanes?\b", re.I)),
    (
        "cable",
        re.compile(r"\b(?:copper\s+cables?|electrical\s+cables?|twinax|active electrical cables?)\b", re.I),
    ),
    (
        "chip_to_chip",
        re.compile(r"\b(?:chip[- ]to[- ]chip|c2c\s+(?:links?|interfaces?|interconnects?))\b", re.I),
    ),
)

ABSTRACT_IMPLEMENTATION_RE = re.compile(
    r"\b(?:we\s+(?:present|demonstrate|propose|report)|this\s+(?:work|paper|design|"
    r"link|interface|receiver|transmitter|transceiver|circuit)|proposed|presented|"
    r"designed|implemented|fabricated|targets?|intended|used\s+(?:for|in))\b",
    re.IGNORECASE,
)
PRIMARY_IMPLEMENTATION_RE = re.compile(
    r"\b(?:we\s+(?:present|demonstrate|propose|report)|"
    r"this\s+(?:work|paper|design|link|interface|receiver|transmitter|"
    r"transceiver|circuit))\b",
    re.IGNORECASE,
)
RELATED_WORK_RE = re.compile(
    r"\b(?:prior|previous(?:ly)?|earlier|existing|recent(?:ly)?|past|"
    r"state[- ]of[- ]the[- ]art|literature|other\s+works?|previous\s+generations?)\b|"
    r"\b(?:has|have|had|were|was)\s+been\s+"
    r"(?:proposed|presented|demonstrated|implemented|used)\b",
    re.IGNORECASE,
)


def _result(value: str, source: str, confidence: float, reasons: list[str],
            evidence: str | None, version: str, key: str) -> dict:
    return {
        key: value,
        "source": source,
        "confidence": float(confidence),
        "reason_codes": reasons,
        "evidence": plain_text(evidence)[:1000] if evidence else None,
        "classifier_version": version,
    }


def _scope_from_text(text: str) -> tuple[str, str] | None:
    if not text:
        return None
    if PARTIAL_SCOPE_RE.search(text):
        return "unknown", "partial_scope_exclusion"
    if FULL_LINK_SCOPE_RE.search(text):
        return "full_link", "explicit_full_link"
    if TRX_SCOPE_RE.search(text):
        return "trx", "explicit_transceiver"
    if TRX_SYSTEM_HEAD_RE.search(text):
        return "trx", "system_transceiver_head"
    if DRIVER_ONLY_SCOPE_RE.search(text):
        return "driver_only", "explicit_driver_only"
    primary_tx = bool(TX_PRIMARY_SCOPE_RE.search(text))
    primary_rx = bool(RX_PRIMARY_SCOPE_RE.search(text))
    if primary_tx and primary_rx:
        return "trx", "tx_rx_primary_cooccurrence"
    if primary_tx:
        return "tx", "explicit_tx"
    if primary_rx:
        return "rx", "explicit_rx"
    if FULL_LINK_HEAD_RE.search(text):
        return "full_link", "system_link_head"
    has_tx = bool(TX_SCOPE_RE.search(text))
    has_rx = bool(RX_SCOPE_RE.search(text))
    if has_tx and has_rx:
        return "trx", "tx_rx_cooccurrence"
    if has_tx:
        return "tx", "explicit_tx"
    if has_rx:
        return "unknown", "receiver_subblock_only"
    return None


def _metric_attributed_scope(text: str) -> tuple[str, str] | None:
    """Require an explicit metric subject before evidence can override title scope."""
    if PARTIAL_SCOPE_RE.search(text):
        return "unknown", "partial_scope_exclusion"
    if FULL_LINK_SCOPE_RE.search(text):
        return "full_link", "explicit_full_link"
    if re.search(
        r"(?:\b(?:full\s+)?transceivers?\b.{0,70}\b(?:consum\w*|dissipat\w*|"
        r"energy|power|[pf]j\s*/)|\b(?:energy|power|[pf]j\s*/).{0,70}"
        r"\b(?:full\s+)?transceivers?\b)", text, re.I,
    ):
        return "trx", "metric_attributed_transceiver"
    if re.search(
        r"\b(?:receivers?|rx(?:[- ]only)?)\b\s+(?:consum\w*|dissipat\w*|"
        r"achiev\w*|require\w*|use[sd]?)", text, re.I,
    ):
        return "rx", "metric_attributed_rx"
    if re.search(
        r"\b(?:transmitters?|tx(?:[- ]only)?)\b\s+(?:consum\w*|dissipat\w*|"
        r"achiev\w*|require\w*|use[sd]?)", text, re.I,
    ):
        return "tx", "metric_attributed_tx"
    driver = DRIVER_ONLY_SCOPE_RE.search(text)
    if driver and re.search(
        r"\b(?:consum\w*|dissipat\w*|achiev\w*|require\w*|use[sd]?|"
        r"energy|power|[pf]j\s*/)", text[driver.end():driver.end() + 70], re.I,
    ):
        return "driver_only", "metric_attributed_driver"
    return None


def classify_energy_component_scope(
    title: str | None,
    evidence_texts: Iterable[str] | str | None = None,
    stored_component_scope: str | None = None,
) -> dict:
    """Classify what circuitry the reported/calculated energy covers.

    This does not reuse ``serdes_measurements.energy_scope`` because that field
    currently records value provenance (reported vs. calculated), not circuit
    coverage.
    """
    if isinstance(evidence_texts, str):
        evidence_values = [evidence_texts]
    else:
        evidence_values = list(evidence_texts or ())
    for value in evidence_values:
        text = plain_text(value)
        if not ENERGY_OR_POWER_RE.search(text):
            continue
        classified = _metric_attributed_scope(text)
        if classified:
            scope, reason = classified
            return _result(
                scope, "evidence", 0.94, [reason, "metric_local_evidence"],
                text, ENERGY_COMPONENT_SCOPE_VERSION, "energy_component_scope",
            )

    title_text = plain_text(title)
    classified = _scope_from_text(title_text)
    if classified:
        scope, reason = classified
        confidence = 0.90 if scope in {"full_link", "driver_only", "trx"} else 0.84
        return _result(
            scope, "title", confidence, [reason], title_text,
            ENERGY_COMPONENT_SCOPE_VERSION, "energy_component_scope",
        )

    stored = str(stored_component_scope or "").strip().lower()
    stored_map = {
        "tx": "tx", "rx": "rx", "tx_rx": "trx", "trx": "trx",
        "full_link": "full_link", "driver_only": "driver_only",
    }
    if stored in stored_map:
        return _result(
            stored_map[stored], "measurement", 0.70,
            ["stored_component_scope"], stored,
            ENERGY_COMPONENT_SCOPE_VERSION, "energy_component_scope",
        )
    return _result(
        "unknown", "none", 0.0, ["insufficient_scope_evidence"], None,
        ENERGY_COMPONENT_SCOPE_VERSION, "energy_component_scope",
    )


def _implementation_sentence(text_value: str | None, pattern: re.Pattern) -> str | None:
    text = plain_text(text_value)
    for sentence in re.split(r"(?<=[.!?;])\s+", text):
        primary = PRIMARY_IMPLEMENTATION_RE.search(sentence)
        if RELATED_WORK_RE.search(sentence) and not primary:
            continue
        if pattern.search(sentence) and ABSTRACT_IMPLEMENTATION_RE.search(sentence):
            return sentence[:1000]
    return None


def classify_link_subtype(
    title: str | None,
    abstract: str | None,
    link_medium: str | None,
    stored_link_class: str | None = None,
    medium_evidence: str | None = None,
) -> dict:
    """Classify a medium-specific primary link subtype conservatively."""
    medium = str(link_medium or "").strip().lower()
    if medium not in {"optical", "electrical"}:
        return _result(
            "mixed_or_unknown", "medium", 0.0, ["medium_not_specific"], medium,
            LINK_SUBTYPE_VERSION, "link_subtype",
        )
    patterns = OPTICAL_SUBTYPE_PATTERNS if medium == "optical" else ELECTRICAL_SUBTYPE_PATTERNS
    title_text = plain_text(title)
    matches = [(name, pattern) for name, pattern in patterns if pattern.search(title_text)]
    if len(matches) == 1:
        name, _ = matches[0]
        return _result(
            name, "title", 0.94, [f"explicit_{name}_title"], title_text,
            LINK_SUBTYPE_VERSION, "link_subtype",
        )
    if len(matches) > 1:
        return _result(
            "optical_other" if medium == "optical" else "electrical_other",
            "title", 0.45, ["multiple_subtype_title"] + [name for name, _ in matches],
            title_text, LINK_SUBTYPE_VERSION, "link_subtype",
        )

    # The medium classifier already stores the implementation-local sentence
    # that justified Optical/Electrical. Reusing that sentence is safer than
    # searching a full abstract, where related-work and protocol lists caused
    # repeatable false positives (e.g. HBM ESD or prior Si-photonic work).
    abstract_matches = []
    for name, pattern in patterns:
        sentence = _implementation_sentence(medium_evidence, pattern)
        if sentence:
            abstract_matches.append((name, sentence))
    if len(abstract_matches) == 1:
        name, sentence = abstract_matches[0]
        return _result(
            name, "medium_evidence", 0.86, [f"implementation_{name}_medium_evidence"],
            sentence, LINK_SUBTYPE_VERSION, "link_subtype",
        )
    if len(abstract_matches) > 1:
        return _result(
            "optical_other" if medium == "optical" else "electrical_other",
            "medium_evidence", 0.42, ["multiple_subtype_medium_evidence"] + [x[0] for x in abstract_matches],
            abstract_matches[0][1], LINK_SUBTYPE_VERSION, "link_subtype",
        )

    default_value = "optical_other" if medium == "optical" else "electrical_other"
    return _result(
        default_value, "medium", 0.55, ["medium_specific_default"], medium,
        LINK_SUBTYPE_VERSION, "link_subtype",
    )


def evidence_tier(source_kind: Any, review_status: Any = None) -> str:
    """Map measurement provenance to the user-facing confidence tiers."""
    source = str(source_kind or "").strip().lower()
    review = str(review_status or "").strip().lower()
    if review in {"verified", "reviewed", "curated_reference"} or source in {"manual", "pdf", "reference_xlsx"}:
        return "verified"
    if source == "user_sheet":
        return "user_sheet"
    if source == "abstract":
        return "abstract"
    if source == "title":
        return "title"
    return "screening_inference"


def performance_completeness(
    performance: dict | None,
    *,
    has_abstract: bool = False,
    has_pdf: bool = False,
    energy_component_scope: str | None = None,
) -> dict:
    """Return explicit field-presence flags and a non-imputed score."""
    p = performance or {}
    has_rate = any(p.get(field) is not None for field in (
        "lane_rate_gbps", "aggregate_rate_gbps", "reported_rate_gbps",
        "reported_rate_min_gbps", "reported_rate_max_gbps", "symbol_rate_gbaud",
    ))
    has_comparable_rate = (
        p.get("lane_rate_gbps") is not None
        or p.get("aggregate_rate_gbps") is not None
        or (
            p.get("reported_rate_gbps") is not None
            and p.get("rate_scope") in {"lane", "aggregate"}
        )
    )
    flags = {
        "rate": bool(has_rate),
        "energy": p.get("energy_pj_bit") is not None,
        "process": p.get("process_nm") is not None,
        "loss": p.get("channel_loss_db") is not None,
        "scope": str(energy_component_scope or "unknown") != "unknown",
        "abstract": bool(has_abstract),
        "pdf": bool(has_pdf),
    }
    count = sum(flags.values())
    fom_ready = bool(
        has_comparable_rate
        and all(flags[key] for key in ("energy", "process", "scope"))
    )
    status = "fom_ready" if fom_ready else "partial" if count else "metadata_only"
    return {
        **flags,
        "comparable_rate": bool(has_comparable_rate),
        "fom_ready": fom_ready,
        "available": count,
        "total": len(flags),
        "score": count / len(flags),
        "status": status,
    }


def pareto_frontier_ids(
    points: Iterable[dict],
    *,
    id_getter: Callable[[dict], Any],
    x_getter: Callable[[dict], float | None],
    y_getter: Callable[[dict], float | None],
    group_getter: Callable[[dict], Any],
) -> set:
    """Return non-dominated IDs for x=maximize, y=minimize within each group."""
    grouped: dict[Any, list[tuple[Any, float, float]]] = defaultdict(list)
    for point in points:
        x, y = x_getter(point), y_getter(point)
        group = group_getter(point)
        if x is None or y is None or group is None:
            continue
        x, y = float(x), float(y)
        if not math.isfinite(x) or not math.isfinite(y) or x <= 0 or y <= 0:
            continue
        grouped[group].append((id_getter(point), x, y))
    frontier = set()
    for rows in grouped.values():
        for candidate_id, candidate_x, candidate_y in rows:
            dominated = any(
                other_x >= candidate_x
                and other_y <= candidate_y
                and (other_x > candidate_x or other_y < candidate_y)
                for other_id, other_x, other_y in rows
                if other_id != candidate_id
            )
            if not dominated:
                frontier.add(candidate_id)
    return frontier
