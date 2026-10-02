# -*- coding: utf-8 -*-
"""
IEEE Paper Repository - Flask 서버

엔드포인트:
  GET /                    index.html
  GET /api/meta            필터용 메타(연도/출처 목록, 전체/PDF 개수)
  GET /api/papers          검색/필터/페이징된 논문 목록
  GET /api/recommendations 즐겨찾기 제목 기반 관련 논문 추천 (MySQL FULLTEXT)
  GET /pdf/<article_number>  로컬 PDF 서빙 (source_system 컬럼 기준으로 폴더 결정)

실행:  .venv/Scripts/python.exe web/app.py
"""
import hashlib
import os
import re
import secrets
import sys
import threading
import time
from collections import Counter, defaultdict, deque
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import zip_longest
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from flask import (
    Flask, abort, jsonify, redirect, render_template, request, send_file,
    session, url_for, g,
)
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from dotenv import load_dotenv
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from serdes_metrics import (  # noqa: E402
    SERDES_SQL_PATTERN,
    canonical_ieee_url,
    classify_link_medium,
    taxonomy as extract_serdes_taxonomy,
)
from serdes_fom import FOM_ENERGY_BASIS  # noqa: E402
from account_store import AccountStore, current_account_id, favorite_sql  # noqa: E402
from serdes_enrichment import evidence_tier, pareto_frontier_ids  # noqa: E402
from recommendation_policy import (FavoriteSimilarity, MIN_SIMILARITY, TOTAL_RECOMMENDATIONS,
                                   allocate, weighted_profile, excluded_electrical_multicarrier,
                                   classify_interest_topic)  # noqa: E402

IEEE_PDF_DIR = os.path.join(ROOT, "ieee-pdf")      # IEEE(숫자 키) PDF
OPTICA_PDF_DIR = os.path.join(ROOT, "optica-pdf")  # Optica(uri 키) PDF
NATURE_PDF_DIR = os.path.join(ROOT, "nature-pdf")  # Nature(articles/<키>) PDF
DESIGNCON_PDF_DIR = os.path.join(ROOT, "DesignCon")
load_dotenv(os.path.join(ROOT, ".env"))

DB_URL = (
    "mysql+pymysql://{user}:{pw}@{host}:{port}/{db}?charset=utf8mb4".format(
        user=os.getenv("DB_USER", "root"),
        pw=os.getenv("DB_PASSWORD", ""),
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=os.getenv("DB_PORT", "3306"),
        db=os.getenv("DB_NAME", "ieee_repo"),
    )
)
engine = create_engine(DB_URL, pool_pre_ping=True, pool_recycle=3600)

app = Flask(__name__)
app.config['ACCOUNT_AUTH_ENABLED'] = os.getenv('ACCOUNT_AUTH_ENABLED', '1') == '1'
accounts = AccountStore(engine)
AUTH_USERNAME = os.getenv("AUTH_USERNAME", "").strip()
AUTH_PASSWORD_HASH = os.getenv("AUTH_PASSWORD_HASH", "").strip()
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin").strip()
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "").strip()
APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "").strip()
if not AUTH_USERNAME or not AUTH_PASSWORD_HASH or not APP_SECRET_KEY:
    raise RuntimeError(
        "AUTH_USERNAME, AUTH_PASSWORD_HASH, APP_SECRET_KEY must be configured in .env"
    )
if ADMIN_PASSWORD_HASH and secrets.compare_digest(AUTH_USERNAME, ADMIN_USERNAME):
    raise RuntimeError("AUTH_USERNAME and ADMIN_USERNAME must be different")

AUTH_ACCOUNTS = {
    AUTH_USERNAME: {
        "username": AUTH_USERNAME,
        "password_hash": AUTH_PASSWORD_HASH,
        "role": "viewer",
    },
}
if ADMIN_PASSWORD_HASH:
    AUTH_ACCOUNTS[ADMIN_USERNAME] = {
        "username": ADMIN_USERNAME,
        "password_hash": ADMIN_PASSWORD_HASH,
        "role": "admin",
    }

app.secret_key = APP_SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "1") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=64 * 1024,
    TEMPLATES_AUTO_RELOAD=os.getenv("TEMPLATES_AUTO_RELOAD", "1") == "1",
)
SURVEY_AI_UI_ENABLED = os.getenv("SURVEY_AI_UI_ENABLED", "0").strip().lower() in {
    "1", "true", "yes", "on",
}
# ngrok terminates HTTPS and forwards the original scheme/host in X-Forwarded-*.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 5
_login_failures = defaultdict(deque)

SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9._-]+$")      # 파일 경로용 키: 영문/숫자/.-_ 만 (경로조작 차단)
PDF_DIR_BY_PUB = {
    "ieee": IEEE_PDF_DIR,
    "optica": OPTICA_PDF_DIR,
    "nature": NATURE_PDF_DIR,
    "designcon": DESIGNCON_PDF_DIR,
}

PUBLISHERS = [
    {"code": "ieee", "label": "IEEE"},
    {"code": "optica", "label": "Optica"},
    {"code": "nature", "label": "Nature"},
    {"code": "designcon", "label": "DesignCon"},
]

# The SerDes survey is intentionally derived from the existing papers table so it
# remains useful before a separately curated benchmark dataset exists.  The
# expression is deliberately stricter than a generic RX/TX search: a paper must
# contain a SerDes-, wireline-, modulation-, equalization-, or data-rate-specific
# phrase to enter the survey candidate set.
SERDES_TEXT_SQL = "LOWER(CONCAT_WS(' ', title, source_name, issue))"
SERDES_SOURCE_SYSTEMS = ("ieee", "optica")


def _serdes_source_sql(alias=""):
    """The survey source boundary, shared by lists, charts, and evidence."""
    return f"{alias + '.' if alias else ''}source_system IN ('ieee', 'optica')"


def _serdes_paper_url(article_number, source_system, stored_url=None):
    if source_system == "optica":
        return str(stored_url or "")
    return canonical_ieee_url(article_number, stored_url)


SERDES_LATEST_COMPLETE_RUNS_SQL = (
    "SELECT MAX(latest_run.id) FROM serdes_screening_runs latest_run "
    "WHERE latest_run.status='complete' "
    "AND latest_run.scope_name LIKE 'all%' GROUP BY latest_run.venue"
)
SERDES_SCOPE_SQL = (
    "(EXISTS ("
    "SELECT 1 FROM serdes_paper_screenings scope_screen "
    "WHERE scope_screen.paper_id=papers.id "
    f"AND scope_screen.run_id IN ({SERDES_LATEST_COMPLETE_RUNS_SQL}) "
    "AND scope_screen.include_in_survey=1) "
    "OR (papers.source_system='ieee' AND NOT EXISTS (SELECT 1 FROM serdes_paper_screenings audited_screen "
    "WHERE audited_screen.paper_id=papers.id "
    f"AND audited_screen.run_id IN ({SERDES_LATEST_COMPLETE_RUNS_SQL})) "
    f"AND ({SERDES_TEXT_SQL} REGEXP :serdes_pattern)))"
)
SERDES_INCLUDED_SCOPE_SQL = (
    "EXISTS (SELECT 1 FROM serdes_paper_screenings included_screen "
    "WHERE included_screen.paper_id=papers.id "
    f"AND included_screen.run_id IN ({SERDES_LATEST_COMPLETE_RUNS_SQL}) "
    "AND included_screen.include_in_survey=1)"
)
SERDES_SIGNAL_SQL_PATTERNS = {
    "pam4": r"pam[- ]?4|4[- ]?pam",
    "nrz": r"(^|[^a-z0-9])nrz([^a-z0-9]|$)|non[- ]return[- ]to[- ]zero",
    "pam3": r"pam[- ]?3|3[- ]?pam|duobinary",
    "pam8": r"pam[- ]?8|8[- ]?pam",
}
SERDES_BLOCK_SQL_PATTERNS = {
    "tx": r"transmitter|(^|[^a-z])tx([^a-z]|$)|serializer|driver|dac[- ]based",
    "rx": r"receiver|(^|[^a-z])rx([^a-z]|$)|deserializer|front[- ]end|slicer",
    "cdr": r"clock.{0,8}data recovery|clock recovery|(^|[^a-z])cdr([^a-z]|$)",
    "equalizer": (
        r"equaliz|decision.feedback|feed.forward|(^|[^a-z])dfe([^a-z]|$)|"
        r"(^|[^a-z])ffe([^a-z]|$)|(^|[^a-z])ctle([^a-z]|$)"
    ),
    "system": r"serdes|transceiver|serial link|chip[- ]to[- ]chip|die[- ]to[- ]die",
}
def _serdes_taxonomy(title):
    """Return typed, title-grounded SerDes labels and conservative clues."""
    return extract_serdes_taxonomy(title)


def _serdes_where(args):
    """Build the fixed-field SerDes filter without interpolating user input."""
    screening = (args.get("screening") or "").strip().lower()
    screening_classes = {"core", "adjacent", "needs_review", "out_of_scope"}
    where = [_serdes_source_sql()]
    params = {}

    # The default explorer remains a focused SerDes candidate set. Once an
    # explicit screening decision is selected, the audit table becomes the
    # scope so every PTL record (including noise and manual review) is visible.
    if screening not in screening_classes | {"included", "screened"}:
        where.append(SERDES_SCOPE_SQL)
        params["serdes_pattern"] = SERDES_SQL_PATTERN

    q = (args.get("q") or "").strip()
    if q:
        params["like"] = f"%{esc_like(q)}%"
        where.append(
            "(title LIKE :like ESCAPE '\\\\' OR authors LIKE :like ESCAPE '\\\\')"
        )

    source = (args.get("source") or "").strip()
    if source:
        params["source"] = source
        where.append("source_name = :source")

    for key, operator in (("year_from", ">="), ("year_to", "<=")):
        value = (args.get(key) or "").strip()
        if value.isdigit() and 1900 <= int(value) <= date.today().year + 2:
            params[key] = int(value)
            where.append(f"CAST(year AS UNSIGNED) {operator} :{key}")

    signal = (args.get("signal") or "").strip().lower()
    if signal in SERDES_SIGNAL_SQL_PATTERNS:
        params["signal_pattern"] = SERDES_SIGNAL_SQL_PATTERNS[signal]
        where.append(f"({SERDES_TEXT_SQL} REGEXP :signal_pattern)")

    block = (args.get("block") or "").strip().lower()
    if block in SERDES_BLOCK_SQL_PATTERNS:
        params["block_pattern"] = SERDES_BLOCK_SQL_PATTERNS[block]
        where.append(f"({SERDES_TEXT_SQL} REGEXP :block_pattern)")

    medium = (args.get("medium") or "").strip().lower()
    if medium in {"optical", "electrical", "unspecified"}:
        params["link_medium"] = medium
        where.append(
            "COALESCE((SELECT medium_override.link_medium "
            "FROM serdes_paper_subtype_overrides medium_override "
            "WHERE medium_override.paper_id=papers.id "
            "AND medium_override.review_status IN ('verified','reviewed','approved','active') "
            "LIMIT 1), (SELECT medium_filter.link_medium "
            "FROM serdes_paper_link_media medium_filter "
            "WHERE medium_filter.paper_id=papers.id LIMIT 1), 'unspecified') "
            "= :link_medium"
        )

    subtype = (args.get("subtype") or "").strip().lower()
    valid_subtypes = {
        "vcsel", "silicon_photonic", "eml_dml", "pon", "optical_other",
        "die_to_die", "memory_io", "backplane", "cable", "chip_to_chip",
        "electrical_other", "mixed_or_unknown",
    }
    if subtype in valid_subtypes:
        params["link_subtype"] = subtype
        where.append(
            "COALESCE((SELECT subtype_override.link_subtype "
            "FROM serdes_paper_subtype_overrides subtype_override "
            "WHERE subtype_override.paper_id=papers.id "
            "AND subtype_override.review_status IN ('verified','reviewed','approved','active') "
            "LIMIT 1), "
            "(SELECT subtype_filter.link_subtype "
            "FROM serdes_paper_link_subtypes subtype_filter "
            "WHERE subtype_filter.paper_id=papers.id LIMIT 1), "
            "'mixed_or_unknown') = :link_subtype"
        )

    tier = (args.get("evidence_tier") or "").strip().lower()
    tier_conditions = {
        "verified": (
            "(m_filter.review_status IN ('verified','reviewed','curated_reference') "
            "OR m_filter.source_kind IN ('manual','pdf','reference_xlsx'))"
        ),
        "user_sheet": "m_filter.source_kind='user_sheet'",
        "abstract": "m_filter.source_kind='abstract'",
        "title": "m_filter.source_kind='title'",
    }
    if tier in tier_conditions:
        where.append(
            "EXISTS (SELECT 1 FROM serdes_implementation_papers ip_filter "
            "JOIN serdes_measurements m_filter "
            "  ON m_filter.implementation_id=ip_filter.implementation_id "
            "WHERE ip_filter.paper_id=papers.id "
            "AND m_filter.review_status NOT IN ('rejected','invalid','needs_review') "
            f"AND {tier_conditions[tier]})"
        )
    elif tier == "screening_inference":
        where.append(
            "NOT EXISTS (SELECT 1 FROM serdes_implementation_papers ip_filter "
            "JOIN serdes_measurements m_filter "
            "  ON m_filter.implementation_id=ip_filter.implementation_id "
            "WHERE ip_filter.paper_id=papers.id "
            "AND m_filter.review_status NOT IN ('rejected','invalid','needs_review'))"
        )

    if screening in screening_classes:
        params["screening"] = screening
        where.append(
            "EXISTS (SELECT 1 FROM serdes_paper_screenings filter_screen "
            "WHERE filter_screen.paper_id=papers.id "
            f"AND filter_screen.run_id IN ({SERDES_LATEST_COMPLETE_RUNS_SQL}) "
            "AND filter_screen.relevance_class=:screening)"
        )
    elif screening == "included":
        where.append(
            "EXISTS (SELECT 1 FROM serdes_paper_screenings filter_screen "
            "WHERE filter_screen.paper_id=papers.id "
            f"AND filter_screen.run_id IN ({SERDES_LATEST_COMPLETE_RUNS_SQL}) "
            "AND filter_screen.include_in_survey=1)"
        )
    elif screening == "screened":
        where.append(
            "EXISTS (SELECT 1 FROM serdes_paper_screenings filter_screen "
            "WHERE filter_screen.paper_id=papers.id "
            f"AND filter_screen.run_id IN ({SERDES_LATEST_COMPLETE_RUNS_SQL}))"
        )

    if args.get("pdf_only") in ("1", "true", "on", "yes"):
        where.append("pdf_available = 1")

    return " AND ".join(where), params


SERDES_MEASUREMENT_FIELDS = (
    "process_text", "process_nm", "reported_rate_text", "reported_rate_gbps",
    "reported_rate_min_gbps", "reported_rate_max_gbps", "rate_scope",
    "lane_rate_gbps", "lane_count", "aggregate_rate_gbps",
    "aggregate_rate_basis", "symbol_rate_gbaud",
    "throughput_density_gbps_per_mm", "power_mw", "power_scope",
    "energy_pj_bit", "energy_scope", "energy_basis",
    "energy_loss_normalized_pj_bit_db", "channel_loss_db",
    "loss_frequency_ghz", "ber", "ber_scope", "active_area_mm2",
    "link_class", "component_scope", "modulation",
)
SERDES_INFORMATIVE_FIELDS = (
    "process_nm", "reported_rate_gbps", "reported_rate_min_gbps",
    "reported_rate_max_gbps", "lane_rate_gbps", "lane_count",
    "aggregate_rate_gbps", "symbol_rate_gbaud",
    "throughput_density_gbps_per_mm", "power_mw", "energy_pj_bit",
    "energy_loss_normalized_pj_bit_db", "channel_loss_db",
    "loss_frequency_ghz", "ber", "active_area_mm2",
)
SERDES_SOURCE_PRIORITY = {
    "manual": 60,
    "user_sheet": 55,
    "pdf": 50,
    "reference_xlsx": 40,
    "abstract": 30,
    "title": 10,
}
SERDES_SOURCE_LABELS = {
    "manual": "Manual",
    "user_sheet": "User Survey",
    "pdf": "PDF",
    "reference_xlsx": "WLink 2025",
    "abstract": "Abstract",
    "title": "Title",
    "derived_fom": "P/R Calculated",
}
SERDES_BICMOS_RE = re.compile(r"\b(?:bi[\s-]?cmos|si[\s-]*ge)\b", re.IGNORECASE)
SERDES_CMOS_RE = re.compile(r"\b(?:cmos|finfet|fd[\s-]?soi|soi)\b", re.IGNORECASE)


def _serdes_technology_family(*values):
    """Classify only explicit process-family evidence; never infer from node size."""
    corpus = " ".join(str(value) for value in values if value).strip()
    if SERDES_BICMOS_RE.search(corpus):
        return "BiCMOS"
    if SERDES_CMOS_RE.search(corpus):
        return "CMOS"
    return "Other / unspecified"


def _json_scalar(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _measurement_rank(row):
    populated = sum(row.get(field) is not None for field in SERDES_INFORMATIVE_FIELDS)
    review_rank = {
        "verified": 4, "reviewed": 3, "curated_reference": 2, "extracted": 1,
    }.get(row.get("review_status"), 0)
    return (
        review_rank,
        SERDES_SOURCE_PRIORITY.get(row.get("source_kind"), 0),
        populated,
        float(row.get("overall_confidence") or 0),
        int(row.get("evidence_count") or 0),
        row.get("updated_at") or datetime.min,
        int(row.get("id") or row.get("measurement_id") or 0),
    )


def _measurement_payload(row):
    if not row:
        return None
    payload = {field: _json_scalar(row.get(field)) for field in SERDES_MEASUREMENT_FIELDS}
    payload.update({
        "measurement_id": row.get("id"),
        "source_kind": row.get("source_kind"),
        "source_label": SERDES_SOURCE_LABELS.get(
            row.get("source_kind"), row.get("source_kind") or "Unknown"
        ),
        "review_status": row.get("review_status"),
        "evidence_tier": evidence_tier(
            row.get("source_kind"), row.get("review_status")
        ),
        "confidence": _json_scalar(row.get("overall_confidence")),
        "evidence_count": int(row.get("evidence_count") or 0),
        "updated_at": _json_scalar(row.get("updated_at")),
        "energy_component_scope": row.get("energy_component_scope") or "unknown",
        "scope_source": row.get("scope_source"),
        "scope_confidence": _json_scalar(row.get("scope_confidence")),
        "scope_reason_codes": row.get("scope_reason_codes"),
        "scope_evidence": row.get("scope_evidence"),
    })
    return payload


def _measurement_matches_requested_tier(row, requested_tier):
    """Match explorer filters while allowing provenance and review tiers to overlap."""
    if requested_tier in {"user_sheet", "abstract", "title"}:
        return str(row.get("source_kind") or "").lower() == requested_tier
    if requested_tier == "verified":
        return evidence_tier(
            row.get("source_kind"), row.get("review_status")
        ) == "verified"
    return requested_tier != "screening_inference"


def _best_measurements_for_papers(connection, paper_ids, requested_tier=None):
    paper_ids = [int(value) for value in paper_ids]
    if not paper_ids:
        return {}
    params = {f"paper_{index}": value for index, value in enumerate(paper_ids)}
    placeholders = ", ".join(f":paper_{index}" for index in range(len(paper_ids)))
    rows = connection.execute(text(
        "SELECT ip.paper_id, m.*, COALESCE(ec.evidence_count, 0) evidence_count, "
        "COALESCE(scope_override.energy_component_scope, metric_scope.energy_component_scope, 'unknown') energy_component_scope, "
        "CASE WHEN scope_override.measurement_id IS NOT NULL THEN scope_override.override_source ELSE metric_scope.scope_source END scope_source, "
        "CASE WHEN scope_override.measurement_id IS NOT NULL THEN 1.0 ELSE metric_scope.scope_confidence END scope_confidence, "
        "CASE WHEN scope_override.measurement_id IS NOT NULL THEN scope_override.reason ELSE metric_scope.reason_codes END scope_reason_codes, "
        "CASE WHEN scope_override.measurement_id IS NOT NULL THEN scope_override.evidence_text ELSE metric_scope.evidence_text END scope_evidence "
        "FROM serdes_implementation_papers ip "
        "JOIN serdes_measurements m ON m.implementation_id=ip.implementation_id "
        "LEFT JOIN serdes_measurement_metric_scopes metric_scope ON metric_scope.measurement_id=m.id "
        "LEFT JOIN serdes_measurement_scope_overrides scope_override ON scope_override.measurement_id=m.id "
        " AND scope_override.review_status IN ('verified','reviewed','approved','active') "
        "LEFT JOIN (SELECT measurement_id, COUNT(*) evidence_count "
        "           FROM serdes_measurement_evidence GROUP BY measurement_id) ec "
        "  ON ec.measurement_id=m.id "
        f"WHERE ip.paper_id IN ({placeholders})"
    ), params).mappings().all()
    requested_tier = str(requested_tier or "").strip().lower()
    valid_tiers = {
        "verified", "user_sheet", "abstract", "title", "screening_inference",
    }
    if requested_tier not in valid_tiers:
        requested_tier = None
    best = {}
    for row in rows:
        if row.get("review_status") in {"rejected", "invalid", "needs_review"}:
            continue
        if requested_tier and not _measurement_matches_requested_tier(
            row, requested_tier,
        ):
            continue
        paper_id = int(row["paper_id"])
        if paper_id not in best or _measurement_rank(row) > _measurement_rank(best[paper_id]):
            best[paper_id] = row
    return {paper_id: _measurement_payload(row) for paper_id, row in best.items()}


def _paper_completeness_for_papers(connection, paper_rows):
    """Aggregate field presence across every accepted measurement per paper."""
    paper_map = {int(row["id"]): row for row in paper_rows}
    if not paper_map:
        return {}
    params = {f"paper_{index}": value for index, value in enumerate(paper_map)}
    placeholders = ", ".join(f":paper_{index}" for index in range(len(paper_map)))
    rows = connection.execute(text(
        "SELECT ip.paper_id, m.*, "
        "COALESCE(scope_override.energy_component_scope, metric_scope.energy_component_scope, 'unknown') energy_component_scope "
        "FROM serdes_implementation_papers ip "
        "JOIN serdes_measurements m ON m.implementation_id=ip.implementation_id "
        "LEFT JOIN serdes_measurement_metric_scopes metric_scope ON metric_scope.measurement_id=m.id "
        "LEFT JOIN serdes_measurement_scope_overrides scope_override ON scope_override.measurement_id=m.id "
        " AND scope_override.review_status IN ('verified','reviewed','approved','active') "
        f"WHERE ip.paper_id IN ({placeholders}) "
        "AND m.review_status NOT IN ('rejected','invalid','needs_review')"
    ), params).mappings().all()
    result = {}
    for paper_id, paper in paper_map.items():
        result[paper_id] = {
            "rate": 0, "energy": 0, "process": 0, "loss": 0,
            "scope": 0, "abstract": bool(paper.get("paper_abstract")),
            "pdf": bool(paper.get("pdf_available")),
            "same_point_fom_ready": False,
            "evidence_tiers": [],
        }
    for row in rows:
        item = result[int(row["paper_id"])]
        state = 1 if row.get("source_kind") == "title" else 2
        has_rate = any(row.get(field) is not None for field in (
            "lane_rate_gbps", "aggregate_rate_gbps", "reported_rate_gbps",
            "reported_rate_min_gbps", "reported_rate_max_gbps", "symbol_rate_gbaud",
        ))
        has_comparable_rate = (
            row.get("lane_rate_gbps") is not None
            or row.get("aggregate_rate_gbps") is not None
            or (
                row.get("reported_rate_gbps") is not None
                and row.get("rate_scope") in {"lane", "aggregate"}
            )
        )
        present = {
            "rate": has_rate,
            "energy": row.get("energy_pj_bit") is not None,
            "process": row.get("process_nm") is not None,
            "loss": row.get("channel_loss_db") is not None,
            "scope": row.get("energy_pj_bit") is not None
            and row.get("energy_component_scope") not in (None, "", "unknown"),
        }
        for field, exists in present.items():
            if exists:
                item[field] = max(item[field], state)
        tier_value = evidence_tier(row.get("source_kind"), row.get("review_status"))
        if tier_value not in item["evidence_tiers"]:
            item["evidence_tiers"].append(tier_value)
        if (
            state == 2 and has_comparable_rate and present["energy"]
            and present["process"] and present["scope"]
        ):
            item["same_point_fom_ready"] = True
    tier_order = {"verified": 0, "user_sheet": 1, "abstract": 2, "title": 3,
                  "screening_inference": 4}
    for item in result.values():
        item["evidence_tiers"].sort(key=lambda value: tier_order.get(value, 99))
        item["structured_fields"] = sum(
            item[field] == 2 for field in ("rate", "energy", "process", "loss")
        )
        item["any_fields"] = sum(
            item[field] > 0 for field in ("rate", "energy", "process", "loss")
        )
        item["status"] = (
            "fom_ready" if item["same_point_fom_ready"]
            else "complete" if item["structured_fields"] == 4
            else "partial" if item["any_fields"] else "metadata_only"
        )
    return result


def _best_rows_for_implementations(
    rows, predicate=lambda row: True, ranker=_measurement_rank,
):
    """Pick one complete operating-point row per implementation.

    The predicate is applied before ranking so a chart never borrows its x value
    from one operating point and its y value from another.
    """
    best = {}
    for row in rows:
        if row.get("review_status") in {"rejected", "invalid", "needs_review"} or not predicate(row):
            continue
        implementation_id = (
            ("family", int(row["family_id"]))
            if row.get("family_id") is not None
            else int(row["implementation_id"])
        )
        if (
            implementation_id not in best
            or ranker(row) > ranker(best[implementation_id])
        ):
            best[implementation_id] = row
    return best


def _energy_measurement_rank(row):
    """Prefer directly reported energy within the same review tier."""
    base = _measurement_rank(row)
    reported_rank = int(row.get("energy_basis") != FOM_ENERGY_BASIS)
    return (base[0], reported_rank, *base[1:])


def _performance_point(row):
    measurement = _measurement_payload(row)
    year = row.get("publication_year") or row.get("paper_year") or row.get("canonical_year")
    title_value = row.get("paper_title") or row.get("reference_title") or row.get("canonical_title")
    venue = row.get("publication_name") or row.get("paper_venue") or row.get("canonical_venue")
    article_number = row.get("article_number")
    source_system = row.get("paper_source_system") or "ieee"
    paper_url = _serdes_paper_url(article_number, source_system, row.get("paper_url"))
    source_url = row.get("evidence_url") or paper_url
    medium = str(row.get("link_medium") or "").strip().lower()
    if medium not in {"optical", "electrical", "unspecified"}:
        decision = classify_link_medium(
            title_value,
            row.get("paper_abstract"),
            [row.get("link_class")] if row.get("link_class") else None,
        )
        medium = decision["link_medium"]
        medium_source = decision["medium_source"]
        medium_confidence = decision["medium_confidence"]
    else:
        medium_source = row.get("medium_source") or "stored"
        medium_confidence = float(row.get("medium_confidence") or 0)
    return dict(
        implementation_id=int(row["implementation_id"]),
        family_id=int(row["family_id"]) if row.get("family_id") is not None else None,
        paper_id=row.get("canonical_paper_id"),
        article_number=article_number,
        source_system=source_system,
        title=title_value,
        authors=row.get("paper_authors") or row.get("first_author"),
        technology_family=_serdes_technology_family(
            row.get("process_text"), row.get("process_evidence"),
            row.get("paper_abstract"), title_value,
        ),
        link_medium=medium,
        medium_source=medium_source,
        medium_confidence=medium_confidence,
        medium_reason_codes=row.get("medium_reason_codes"),
        medium_evidence=row.get("medium_evidence"),
        link_subtype=row.get("link_subtype") or "mixed_or_unknown",
        subtype_source=row.get("subtype_source"),
        subtype_confidence=_json_scalar(row.get("subtype_confidence")),
        subtype_reason_codes=row.get("subtype_reason_codes"),
        subtype_evidence=row.get("subtype_evidence"),
        year=int(year) if year else None,
        venue=venue,
        matched_paper=bool(row.get("canonical_paper_id")),
        url=paper_url or _serdes_paper_url(article_number, source_system, source_url),
        evidence_url=source_url,
        performance=measurement,
    )


def _pareto_group_key(row, mode):
    """Keep rate denominators separate inside otherwise comparable groups."""
    rate_denominator = (
        str(row.get("rate_scope") or "unknown")
        if mode == "reported" else mode
    )
    return (
        row.get("link_medium"), row.get("energy_component_scope"),
        rate_denominator,
    )


def _resolve_pdf_path(source_system, article_number, pdf_local_path=None):
    """Resolve flat and nested PDF paths while keeping each source in its root."""
    pdf_dir = PDF_DIR_BY_PUB.get(source_system)
    if not pdf_dir:
        return None

    source_root = os.path.realpath(pdf_dir)
    if pdf_local_path:
        normalized = str(pdf_local_path).replace("\\", os.sep).replace("/", os.sep)
        candidate = os.path.realpath(os.path.join(ROOT, normalized))
    else:
        candidate = os.path.realpath(
            os.path.join(pdf_dir, f"{article_number}.pdf")
        )

    try:
        if os.path.commonpath([source_root, candidate]) != source_root:
            return None
    except ValueError:
        return None
    if not candidate.lower().endswith(".pdf"):
        return None
    return candidate


def esc_like(s: str) -> str:
    """LIKE 와일드카드(%, _, \\) 이스케이프."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _client_ip():
    return request.remote_addr or "unknown"


def _csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _csrf_is_valid():
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    return bool(expected and supplied and secrets.compare_digest(expected, supplied))


def _safe_next_url(value):
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/") or value.startswith("//"):
        return None
    return value


def _login_is_rate_limited(ip):
    now = time.monotonic()
    failures = _login_failures[ip]
    while failures and now - failures[0] > LOGIN_WINDOW_SECONDS:
        failures.popleft()
    return len(failures) >= LOGIN_MAX_FAILURES


def _authenticate_account(username, password):
    """Return the configured account after constant-time ID and hash checks."""
    if app.config['ACCOUNT_AUTH_ENABLED']:
        return accounts.authenticate(username, password)
    matched = None
    for configured_username, account in AUTH_ACCOUNTS.items():
        if secrets.compare_digest(username, configured_username):
            matched = account
    if matched and check_password_hash(matched["password_hash"], password):
        return matched
    return None


@app.before_request
def require_login():
    g.account_scope_token = current_account_id.set(None)
    if request.endpoint in {"login", "health", "static"}:
        return None
    if session.get('authenticated') and app.config['ACCOUNT_AUTH_ENABLED']:
        try:
            account = accounts.get(session.get('account_id'))
        except SQLAlchemyError:
            return jsonify(ok=False, error='계정 저장소를 사용할 수 없습니다.'), 503
        if (not account or not account['is_active']
                or account['session_version'] != session.get('session_version')):
            session.clear()
        else:
            session['role'] = account['role']
            session['username'] = account['username']
            current_account_id.set(account['id'])
            if account['must_change_password'] and request.endpoint not in {'account_password', 'logout'}:
                if request.path.startswith('/api/'):
                    return jsonify(ok=False, error='password change required'), 403
                return redirect(url_for('account_password'))
    if not session.get("authenticated"):
        if request.path.startswith("/api/"):
            return jsonify(ok=False, error="authentication required"), 401
        return redirect(url_for("login", next=request.full_path if request.query_string else request.path))
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not _csrf_is_valid():
        if request.path.startswith("/api/"):
            return jsonify(ok=False, error="invalid CSRF token"), 403
        abort(403)


@app.teardown_request
def clear_account_scope(error=None):
    token = g.pop('account_scope_token', None)
    if token is not None:
        current_account_id.reset(token)


@app.after_request
def security_headers(response):
    # 메인 문서는 외부/다른 문서의 frame 삽입을 계속 차단한다. 다만 /pdf/*는
    # 같은 origin의 사이드 패널 iframe에서 표시해야 하므로 SAMEORIGIN만 허용한다.
    is_pdf_response = request.path.startswith("/pdf/")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN" if is_pdf_response else "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    frame_ancestors = "'self'" if is_pdf_response else "'none'"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        f"script-src 'self' 'unsafe-inline'; frame-src 'self'; frame-ancestors {frame_ancestors}; "
        "form-action 'self'; base-uri 'self'"
    )
    if request.is_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.path in {"/login", "/logout"} or (
            session.get('authenticated') and 'private' not in response.headers.get('Cache-Control', '')):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/health")
def health():
    return jsonify(ok=True)


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authenticated") and (not app.config['ACCOUNT_AUTH_ENABLED'] or session.get('account_id')):
        return redirect(url_for("index"))

    error = None
    next_url = _safe_next_url(request.values.get("next"))
    if request.method == "POST":
        ip = _client_ip()
        if not _csrf_is_valid():
            abort(403)
        if _login_is_rate_limited(ip):
            error = "로그인 시도가 너무 많습니다. 15분 후 다시 시도하세요."
        else:
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            try:
                account = _authenticate_account(username, password)
            except SQLAlchemyError:
                return render_template('login.html', error='계정 저장소에 연결할 수 없습니다.',
                                       next_url=next_url or '', csrf_token=_csrf_token()), 503
            if account:
                _login_failures.pop(ip, None)
                session.clear()
                session["authenticated"] = True
                session["username"] = account["username"]
                session["role"] = account["role"]
                if app.config['ACCOUNT_AUTH_ENABLED']:
                    session['account_id'] = account['id']
                    session['session_version'] = account['session_version']
                session["csrf_token"] = secrets.token_urlsafe(32)
                session.permanent = True
                return redirect(url_for('account_password') if account.get('must_change_password')
                                else next_url or url_for("index"))
            _login_failures[ip].append(time.monotonic())
            error = "ID 또는 비밀번호가 올바르지 않습니다."

    return render_template(
        "login.html", error=error, next_url=next_url or "", csrf_token=_csrf_token()
    )


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route('/account/password', methods=['GET', 'POST'])
def account_password():
    error = None
    if not current_account_id.get():
        abort(404)
    if request.method == 'POST':
        previous = request.form.get('current_password', '')
        password = request.form.get('new_password', '')
        try:
            if password != request.form.get('confirmation', ''):
                raise ValueError('새 비밀번호와 확인 입력이 일치하지 않습니다.')
            version = accounts.change_password(current_account_id.get(), previous, password)
        except ValueError as exc:
            error = str(exc)
        else:
            session['session_version'] = version
            session['csrf_token'] = secrets.token_urlsafe(32)
            return redirect(url_for('index'))
    return render_template('account_password.html', username=session['username'],
                           error=error, csrf_token=_csrf_token())


@app.route("/")
def index():
    return render_template(
        "index.html",
        username=session.get("username"),
        role=session.get("role", "viewer"),
        can_edit_favorites=session.get("role") in {"admin", "member"},
        csrf_token=_csrf_token(),
    )


@app.route("/serdes")
def serdes_survey():
    return render_template(
        "serdes.html",
        username=session.get("username"),
        role=session.get("role", "viewer"),
        can_edit_favorites=session.get("role") in {"admin", "member"},
        can_edit_shared=session.get('role') == 'admin',
        survey_ai_visible=SURVEY_AI_UI_ENABLED,
        csrf_token=_csrf_token(),
    )


@app.route("/api/serdes/meta")
def api_serdes_meta():
    base_where = (
        f"{_serdes_source_sql()} AND "
        f"{SERDES_INCLUDED_SCOPE_SQL}"
    )
    params = {}

    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT year, source_name, source_type, source_system, "
            f"pdf_available, {favorite_sql()} AS is_favorite, created_at "
            f"FROM papers WHERE {base_where}"
        ), params).mappings().all()
        try:
            audit_facet_rows = c.execute(text(
                "SELECT p.year, p.source_name, p.source_type "
                "FROM papers p "
                "JOIN serdes_paper_screenings audit_screen "
                "  ON audit_screen.paper_id=p.id "
                f" AND audit_screen.run_id IN ({SERDES_LATEST_COMPLETE_RUNS_SQL}) "
                f"WHERE {_serdes_source_sql('p')}"
            )).mappings().all()
            measurement_summary = c.execute(text(
                "SELECT "
                " COUNT(DISTINCT papers.id) population_total, "
                " COUNT(DISTINCT CASE WHEN m.id IS NOT NULL THEN papers.id END) measurement_total, "
                " COUNT(DISTINCT CASE WHEN m.source_kind <> 'title' THEN papers.id END) structured_total, "
                " COUNT(DISTINCT CASE WHEN m.source_kind='title' THEN papers.id END) title_clue_total, "
                " COUNT(DISTINCT CASE WHEN (m.lane_rate_gbps IS NOT NULL OR "
                "   m.aggregate_rate_gbps IS NOT NULL OR m.reported_rate_gbps IS NOT NULL OR "
                "   m.reported_rate_min_gbps IS NOT NULL OR m.reported_rate_max_gbps IS NOT NULL OR "
                "   m.symbol_rate_gbaud IS NOT NULL) THEN papers.id END) rate_total, "
                " COUNT(DISTINCT CASE WHEN m.lane_rate_gbps IS NOT NULL THEN papers.id END) lane_rate_total, "
                " COUNT(DISTINCT CASE WHEN m.reported_rate_gbps IS NOT NULL THEN papers.id END) reported_rate_total, "
                " COUNT(DISTINCT CASE WHEN m.energy_pj_bit IS NOT NULL THEN papers.id END) energy_total, "
                " COUNT(DISTINCT CASE WHEN m.energy_pj_bit IS NOT NULL "
                "   AND (m.energy_basis IS NULL OR m.energy_basis <> :fom_basis) "
                "   THEN papers.id END) reported_energy_total, "
                " COUNT(DISTINCT CASE WHEN m.energy_pj_bit IS NOT NULL "
                "   AND m.energy_basis=:fom_basis THEN papers.id END) calculated_energy_total, "
                " COUNT(DISTINCT CASE WHEN m.energy_pj_bit IS NOT NULL "
                "   AND m.energy_basis=:fom_basis AND NOT EXISTS ("
                "     SELECT 1 FROM serdes_implementation_papers reported_ip "
                "     JOIN serdes_measurements reported_m "
                "       ON reported_m.implementation_id=reported_ip.implementation_id "
                "     WHERE reported_ip.paper_id=papers.id "
                "       AND reported_m.energy_pj_bit IS NOT NULL "
                "       AND (reported_m.energy_basis IS NULL "
                "            OR reported_m.energy_basis<>:fom_basis) "
                "       AND reported_m.review_status NOT IN "
                "           ('rejected','invalid','needs_review')"
                "   ) THEN papers.id END) calculated_only_energy_total, "
                " COUNT(DISTINCT CASE WHEN m.process_nm IS NOT NULL THEN papers.id END) process_total, "
                " COUNT(DISTINCT CASE WHEN m.channel_loss_db IS NOT NULL THEN papers.id END) loss_total, "
                " COUNT(DISTINCT CASE WHEN m.ber IS NOT NULL THEN papers.id END) ber_total, "
                " COUNT(DISTINCT CASE WHEN m.power_mw IS NOT NULL THEN papers.id END) power_total, "
                " COUNT(DISTINCT CASE WHEN m.energy_loss_normalized_pj_bit_db IS NOT NULL "
                "                     THEN papers.id END) normalized_energy_loss_total, "
                " COUNT(DISTINCT CASE WHEN m.throughput_density_gbps_per_mm IS NOT NULL "
                "                     THEN papers.id END) throughput_density_total, "
                " COUNT(DISTINCT CASE WHEN m.energy_pj_bit IS NOT NULL AND "
                "  (m.lane_rate_gbps IS NOT NULL OR m.aggregate_rate_gbps IS NOT NULL OR "
                "   m.reported_rate_gbps IS NOT NULL OR m.symbol_rate_gbaud IS NOT NULL) "
                "  THEN papers.id END) rate_energy_total, "
                " COUNT(DISTINCT CASE WHEN m.energy_pj_bit IS NOT NULL AND m.process_nm IS NOT NULL "
                "  THEN papers.id END) energy_process_total, "
                " COUNT(DISTINCT CASE WHEN m.energy_pj_bit IS NOT NULL AND m.channel_loss_db IS NOT NULL "
                "  THEN papers.id END) energy_loss_total, "
                " COUNT(DISTINCT CASE WHEN m.review_status='verified' THEN papers.id END) verified_total, "
                " MIN(m.publication_year) performance_year_min, "
                " MAX(m.publication_year) performance_year_max, "
                " MAX(m.updated_at) performance_updated_at "
                "FROM papers "
                "LEFT JOIN serdes_implementation_papers ip ON ip.paper_id=papers.id "
                "LEFT JOIN serdes_measurements m ON m.implementation_id=ip.implementation_id "
                " AND m.review_status NOT IN ('rejected','invalid','needs_review') "
                f"WHERE {_serdes_source_sql('papers')} AND "
                f"{SERDES_INCLUDED_SCOPE_SQL}"
            ), {"fom_basis": FOM_ENERGY_BASIS}).mappings().one()
            abstract_total = c.execute(text(
                "SELECT COUNT(DISTINCT a.paper_id) "
                "FROM paper_current_abstracts a "
                "JOIN papers p ON p.id=a.paper_id "
                "JOIN serdes_paper_screenings abstract_screen ON abstract_screen.paper_id=a.paper_id "
                f" AND abstract_screen.run_id IN ({SERDES_LATEST_COMPLETE_RUNS_SQL}) "
                "WHERE a.is_current=1 AND abstract_screen.include_in_survey=1 "
                f"AND {_serdes_source_sql('p')}"
            )).scalar() or 0
            abstract_provider_rows = c.execute(text(
                "SELECT a.provider, COUNT(DISTINCT a.paper_id) count "
                "FROM paper_current_abstracts a "
                "JOIN papers p ON p.id=a.paper_id "
                "JOIN serdes_paper_screenings abstract_screen ON abstract_screen.paper_id=a.paper_id "
                f" AND abstract_screen.run_id IN ({SERDES_LATEST_COMPLETE_RUNS_SQL}) "
                "WHERE a.is_current=1 AND abstract_screen.include_in_survey=1 "
                f"AND {_serdes_source_sql('p')} "
                "GROUP BY a.provider ORDER BY count DESC"
            )).mappings().all()
            technology_rows = c.execute(text(
                "SELECT p.title, a.abstract_text, technology_screen.relevance_class, "
                "COALESCE(subtype_override.link_medium, medium.link_medium, 'unspecified') link_medium, "
                "COALESCE(subtype_override.link_subtype, subtype.link_subtype, 'mixed_or_unknown') link_subtype "
                "FROM papers p "
                "JOIN serdes_paper_screenings technology_screen ON technology_screen.paper_id=p.id "
                f" AND technology_screen.run_id IN ({SERDES_LATEST_COMPLETE_RUNS_SQL}) "
                "LEFT JOIN paper_current_abstracts a ON a.paper_id=p.id AND a.is_current=1 "
                "LEFT JOIN serdes_paper_link_media medium ON medium.paper_id=p.id "
                "LEFT JOIN serdes_paper_link_subtypes subtype ON subtype.paper_id=p.id "
                "LEFT JOIN serdes_paper_subtype_overrides subtype_override ON subtype_override.paper_id=p.id "
                " AND subtype_override.review_status IN ('verified','reviewed','approved','active') "
                f"WHERE {_serdes_source_sql('p')} AND technology_screen.include_in_survey=1"
            )).mappings().all()
            energy_scope_rows = c.execute(text(
                "SELECT COALESCE(scope_override.energy_component_scope, metric_scope.energy_component_scope, 'unknown') energy_component_scope, "
                "COUNT(DISTINCT ip.paper_id) count "
                "FROM serdes_implementation_papers ip "
                "JOIN papers p ON p.id=ip.paper_id "
                "JOIN serdes_paper_screenings scope_screen ON scope_screen.paper_id=ip.paper_id "
                f" AND scope_screen.run_id IN ({SERDES_LATEST_COMPLETE_RUNS_SQL}) "
                "JOIN serdes_measurements m ON m.implementation_id=ip.implementation_id "
                "LEFT JOIN serdes_measurement_metric_scopes metric_scope ON metric_scope.measurement_id=m.id "
                "LEFT JOIN serdes_measurement_scope_overrides scope_override ON scope_override.measurement_id=m.id "
                " AND scope_override.review_status IN ('verified','reviewed','approved','active') "
                "WHERE scope_screen.include_in_survey=1 AND m.energy_pj_bit IS NOT NULL "
                f"AND {_serdes_source_sql('p')} "
                "AND m.review_status NOT IN ('rejected','invalid','needs_review') "
                "GROUP BY energy_component_scope"
            )).mappings().all()
            unmatched_performance_total = c.execute(text(
                "SELECT COUNT(DISTINCT m.implementation_id) "
                "FROM serdes_measurements m "
                "JOIN serdes_implementations i ON i.id=m.implementation_id "
                "WHERE m.source_kind <> 'title' AND i.canonical_paper_id IS NULL"
            )).scalar() or 0
            screening_rows = c.execute(text(
                "SELECT r.venue, r.scope_name, r.scope_version, r.target_count, "
                "r.core_count, r.adjacent_count, r.review_count, "
                "r.excluded_count, r.abstract_count, r.completed_at "
                "FROM serdes_screening_runs r "
                "JOIN (SELECT venue, MAX(id) id FROM serdes_screening_runs "
                "      WHERE status='complete' AND scope_name LIKE 'all%' "
                "      GROUP BY venue) latest "
                "  ON latest.id=r.id "
                "WHERE EXISTS (SELECT 1 FROM serdes_paper_screenings s "
                " JOIN papers p ON p.id=s.paper_id WHERE s.run_id=r.id "
                f" AND {_serdes_source_sql('p')}) ORDER BY r.venue"
            )).mappings().all()
        except SQLAlchemyError:
            audit_facet_rows = []
            measurement_summary = {}
            abstract_total = 0
            abstract_provider_rows = []
            technology_rows = []
            energy_scope_rows = []
            unmatched_performance_total = 0
            screening_rows = []

    year_counts = Counter()
    source_counts = Counter()
    audit_year_counts = Counter()
    audit_source_counts = Counter()
    pdf_total = 0
    fav_total = 0
    updated_at = None
    for row in rows:
        year_value = str(row["year"] or "")
        if re.fullmatch(r"\d{4}", year_value):
            year_counts[int(year_value)] += 1
        if row["source_name"]:
            source_counts[(row["source_name"], row["source_type"])] += 1
        pdf_total += int(bool(row["pdf_available"]))
        fav_total += int(bool(row["is_favorite"]))
        if row["created_at"] and (updated_at is None or row["created_at"] > updated_at):
            updated_at = row["created_at"]

    for row in audit_facet_rows:
        year_value = str(row["year"] or "")
        if re.fullmatch(r"\d{4}", year_value):
            audit_year_counts[int(year_value)] += 1
        if row["source_name"]:
            audit_source_counts[(row["source_name"], row["source_type"])] += 1

    valid_years = sorted(year_counts)
    year_rows = [dict(year=year, count=year_counts[year]) for year in valid_years]
    source_rows = [
        dict(name=name, type=source_type, count=count)
        for (name, source_type), count in sorted(
            source_counts.items(), key=lambda item: (-item[1], item[0][0])
        )
    ]
    audit_year_rows = [
        dict(year=year, count=audit_year_counts[year])
        for year in sorted(audit_year_counts)
    ]
    audit_source_rows = [
        dict(name=name, type=source_type, count=count)
        for (name, source_type), count in audit_source_counts.most_common()
    ]
    technology_counts = Counter(
        _serdes_technology_family(row.get("abstract_text"), row.get("title"))
        for row in technology_rows
    )
    link_medium_counts = Counter()
    link_subtype_counts = Counter()
    for row in technology_rows:
        medium = str(row.get("link_medium") or "").strip().lower()
        if medium not in {"optical", "electrical", "unspecified"}:
            medium = classify_link_medium(
                row.get("title"), row.get("abstract_text"),
                screening_class=row.get("relevance_class"),
            )["link_medium"]
        link_medium_counts[medium] += 1
        link_subtype_counts[row.get("link_subtype") or "mixed_or_unknown"] += 1
    performance_year_min = measurement_summary.get("performance_year_min")
    performance_year_max = measurement_summary.get("performance_year_max")
    survey_years = valid_years + [
        value for value in (performance_year_min, performance_year_max) if value
    ]
    performance_updated_at = measurement_summary.get("performance_updated_at")
    return jsonify(dict(
        total=len(rows),
        pdf_total=pdf_total,
        fav_total=fav_total,
        year_min=min(survey_years) if survey_years else None,
        year_max=max(survey_years) if survey_years else None,
        paper_year_min=valid_years[0] if valid_years else None,
        paper_year_max=valid_years[-1] if valid_years else None,
        performance_year_min=performance_year_min,
        performance_year_max=performance_year_max,
        population_total=int(measurement_summary.get("population_total") or len(rows)),
        performance_total=int(measurement_summary.get("measurement_total") or 0),
        measurement_total=int(measurement_summary.get("measurement_total") or 0),
        structured_total=int(measurement_summary.get("structured_total") or 0),
        title_clue_total=int(measurement_summary.get("title_clue_total") or 0),
        energy_total=int(measurement_summary.get("energy_total") or 0),
        reported_energy_total=int(
            measurement_summary.get("reported_energy_total") or 0
        ),
        calculated_energy_total=int(
            measurement_summary.get("calculated_energy_total") or 0
        ),
        calculated_only_energy_total=int(
            measurement_summary.get("calculated_only_energy_total") or 0
        ),
        rate_total=int(measurement_summary.get("rate_total") or 0),
        lane_rate_total=int(measurement_summary.get("lane_rate_total") or 0),
        reported_rate_total=int(measurement_summary.get("reported_rate_total") or 0),
        process_total=int(measurement_summary.get("process_total") or 0),
        loss_total=int(measurement_summary.get("loss_total") or 0),
        ber_total=int(measurement_summary.get("ber_total") or 0),
        power_total=int(measurement_summary.get("power_total") or 0),
        normalized_energy_loss_total=int(
            measurement_summary.get("normalized_energy_loss_total") or 0
        ),
        throughput_density_total=int(
            measurement_summary.get("throughput_density_total") or 0
        ),
        rate_energy_total=int(measurement_summary.get("rate_energy_total") or 0),
        energy_process_total=int(measurement_summary.get("energy_process_total") or 0),
        energy_loss_total=int(measurement_summary.get("energy_loss_total") or 0),
        verified_total=int(measurement_summary.get("verified_total") or 0),
        abstract_total=int(abstract_total),
        abstract_providers=[
            dict(provider=row["provider"], count=int(row["count"] or 0))
            for row in abstract_provider_rows
        ],
        technology_families=[
            dict(family=family, count=int(technology_counts.get(family, 0)))
            for family in ("CMOS", "BiCMOS", "Other / unspecified")
        ],
        link_media=[
            dict(medium=medium, count=int(link_medium_counts.get(medium, 0)))
            for medium in ("electrical", "optical", "unspecified")
        ],
        link_subtypes=[
            dict(subtype=subtype, count=int(link_subtype_counts.get(subtype, 0)))
            for subtype in (
                "die_to_die", "memory_io", "backplane", "cable", "chip_to_chip",
                "electrical_other", "vcsel", "silicon_photonic", "eml_dml",
                "pon", "optical_other", "mixed_or_unknown",
            )
        ],
        energy_scopes=[
            dict(scope=row["energy_component_scope"], count=int(row["count"] or 0))
            for row in energy_scope_rows
        ],
        unmatched_performance_total=int(unmatched_performance_total),
        updated_at=(performance_updated_at or updated_at).isoformat()
        if (performance_updated_at or updated_at) else None,
        years=year_rows,
        sources=source_rows,
        audit_years=audit_year_rows,
        audit_sources=audit_source_rows,
        screenings=[dict(row) for row in screening_rows],
        source_system="ieee,optica",
        source_systems=list(SERDES_SOURCE_SYSTEMS),
    ))


def _with_bibliography_metadata(item):
    """A real zero is known; only missing primary metadata uses the snapshot."""
    bibliography_authors = item.pop("bibliography_authors", None)
    if not str(item.get("authors") or "").strip():
        item["authors"] = bibliography_authors
    count = item.pop("bibliography_citation_count", None)
    provider = item.pop("bibliography_provider", None)
    fetched_at = item.pop("bibliography_fetched_at", None)
    if item.get("citation_count") is None and count is not None:
        item.update(citation_count=count, citation_source=provider, citation_updated_at=fetched_at)
    return item


@app.route("/api/serdes/papers")
def api_serdes_papers():
    where_sql, params = _serdes_where(request.args)
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        size = min(100, max(1, int(request.args.get("size", 30))))
    except ValueError:
        size = 30

    sort = (request.args.get("sort") or "newest").strip().lower()
    order_sql = {
        "newest": "CAST(p.year AS UNSIGNED) DESC, p.id DESC",
        "citations": "COALESCE(p.citation_count, bibliography.citation_count) IS NULL, COALESCE(p.citation_count, bibliography.citation_count) DESC, CAST(p.year AS UNSIGNED) DESC",
        "title": "p.title, CAST(p.year AS UNSIGNED) DESC",
        "relevance": (
            "s.relevance_score IS NULL, "
            "FIELD(s.relevance_class, 'core', 'adjacent', 'needs_review', 'out_of_scope'), "
            "s.relevance_score DESC, "
            "CAST(p.year AS UNSIGNED) DESC, p.id DESC"
        ),
    }.get(sort, "CAST(p.year AS UNSIGNED) DESC, p.id DESC")
    offset = (page - 1) * size

    with engine.connect() as c:
        total = c.execute(
            text(f"SELECT COUNT(*) FROM papers WHERE {where_sql}"), params,
        ).scalar()
        rows = c.execute(text(
            "SELECT p.*, bibliography.authors bibliography_authors, "
            "bibliography.citation_count bibliography_citation_count, "
            "bibliography.provider bibliography_provider, bibliography.fetched_at bibliography_fetched_at, "
            "s.relevance_class, s.relevance_score, "
            "s.include_in_survey, s.rationale screening_rationale, "
            "s.screening_source, s.review_status screening_review_status, "
            "s.scope_version screening_scope_version, a.abstract_text paper_abstract, "
            "COALESCE(subtype_override.link_medium, medium.link_medium, 'unspecified') link_medium, "
            "CASE WHEN subtype_override.paper_id IS NOT NULL THEN subtype_override.override_source ELSE medium.medium_source END medium_source, "
            "CASE WHEN subtype_override.paper_id IS NOT NULL THEN 1.0 ELSE medium.medium_confidence END medium_confidence, "
            "CASE WHEN subtype_override.paper_id IS NOT NULL THEN subtype_override.reason ELSE medium.reason_codes END medium_reason_codes, "
            "CASE WHEN subtype_override.paper_id IS NOT NULL THEN subtype_override.evidence_text ELSE medium.evidence_text END medium_evidence, "
            "CASE WHEN subtype_override.paper_id IS NOT NULL THEN 'manual_override' ELSE medium.classifier_version END medium_classifier_version, "
            "family_member.family_id, "
            "COALESCE(subtype_override.link_subtype, subtype.link_subtype, 'mixed_or_unknown') link_subtype, "
            "CASE WHEN subtype_override.paper_id IS NOT NULL THEN subtype_override.override_source ELSE subtype.subtype_source END subtype_source, "
            "CASE WHEN subtype_override.paper_id IS NOT NULL THEN 1.0 ELSE subtype.subtype_confidence END subtype_confidence, "
            "CASE WHEN subtype_override.paper_id IS NOT NULL THEN subtype_override.reason ELSE subtype.reason_codes END subtype_reason_codes, "
            "CASE WHEN subtype_override.paper_id IS NOT NULL THEN subtype_override.evidence_text ELSE subtype.evidence_text END subtype_evidence "
            "FROM (SELECT id, article_number, title, authors, year, source_name, "
            f"source_type, source_system, issue, url, pdf_available, {favorite_sql()} AS is_favorite, "
            "citation_count, citation_source, citation_updated_at, doi "
            f"FROM papers WHERE {where_sql}) p "
            "LEFT JOIN serdes_paper_bibliography bibliography ON bibliography.paper_id=p.id "
            "LEFT JOIN serdes_paper_screenings s ON s.paper_id=p.id "
            f" AND s.run_id IN ({SERDES_LATEST_COMPLETE_RUNS_SQL}) "
            "LEFT JOIN paper_current_abstracts a ON a.paper_id=p.id AND a.is_current=1 "
            "LEFT JOIN serdes_paper_link_media medium ON medium.paper_id=p.id "
            "LEFT JOIN serdes_implementation_family_members family_member ON family_member.paper_id=p.id "
            "LEFT JOIN serdes_paper_link_subtypes subtype ON subtype.paper_id=p.id "
            "LEFT JOIN serdes_paper_subtype_overrides subtype_override ON subtype_override.paper_id=p.id "
            " AND subtype_override.review_status IN ('verified','reviewed','approved','active') "
            f"ORDER BY {order_sql} "
            "LIMIT :size OFFSET :offset"
        ), {**params, "size": size, "offset": offset}).mappings().all()
        try:
            best_measurements = _best_measurements_for_papers(
                c, [row["id"] for row in rows],
                requested_tier=request.args.get("evidence_tier"),
            )
            completeness = _paper_completeness_for_papers(c, rows)
        except SQLAlchemyError:
            best_measurements = {}
            completeness = {}

    items = []
    for row in rows:
        item = _with_bibliography_metadata(dict(row))
        item["stored_url"] = item.get("url")
        item["url"] = _serdes_paper_url(
            item.get("article_number"), item.get("source_system"), item.get("url")
        )
        item["taxonomy"] = _serdes_taxonomy(item.get("title"))
        item["performance"] = best_measurements.get(int(item["id"]))
        item["completeness"] = completeness.get(int(item["id"]), {
            "rate": 0, "energy": 0, "process": 0, "loss": 0,
            "scope": 0, "abstract": False, "pdf": bool(item.get("pdf_available")),
            "same_point_fom_ready": False, "evidence_tiers": [],
            "structured_fields": 0, "any_fields": 0, "status": "metadata_only",
        })
        paper_abstract = item.pop("paper_abstract", None)
        medium = str(item.get("link_medium") or "").strip().lower()
        if medium not in {"optical", "electrical", "unspecified"}:
            decision = classify_link_medium(
                item.get("title"), paper_abstract,
                screening_class=item.get("relevance_class"),
            )
            item["link_medium"] = decision["link_medium"]
            item["medium_source"] = decision["medium_source"]
            item["medium_confidence"] = decision["medium_confidence"]
            item["medium_classifier_version"] = decision["medium_classifier_version"]
        else:
            item["medium_confidence"] = float(item.get("medium_confidence") or 0)
        item["technology_family"] = _serdes_technology_family(
            (item["performance"] or {}).get("process_text"),
            paper_abstract,
            item.get("title"),
        )
        screening_values = dict(
            relevance_class=item.pop("relevance_class", None),
            relevance_score=item.pop("relevance_score", None),
            include_in_survey=item.pop("include_in_survey", None),
            rationale=item.pop("screening_rationale", None),
            source=item.pop("screening_source", None),
            review_status=item.pop("screening_review_status", None),
            scope_version=item.pop("screening_scope_version", None),
        )
        item["screening"] = None
        if screening_values["relevance_class"]:
            item["screening"] = {
                **screening_values,
                "relevance_score": float(screening_values["relevance_score"] or 0),
                "include_in_survey": bool(screening_values["include_in_survey"]),
            }
        items.append(item)
    pages = (total + size - 1) // size if total else 0
    return jsonify(dict(
        items=items,
        total=total,
        page=page,
        size=size,
        pages=pages,
    ))


@app.route("/api/serdes/performance")
def api_serdes_performance():
    return jsonify(build_serdes_performance(request.args))


def build_serdes_performance(args, strict=False):
    """Return traceable operating points for the fixed Core + Adjacent corpus."""
    requested_tier = (args.get("evidence_tier") or "structured").lower()
    source_filters = {
        "all": "",
        "structured": "AND m.source_kind <> 'title'",
        "verified": (
            "AND (m.review_status IN ('verified','reviewed','curated_reference') "
            "OR m.source_kind IN ('manual','pdf','reference_xlsx'))"
        ),
        "user_sheet": "AND m.source_kind='user_sheet'",
        "abstract": "AND m.source_kind='abstract'",
        "title": "AND m.source_kind='title'",
    }
    if requested_tier not in source_filters:
        requested_tier = "structured"
    source_filter = source_filters[requested_tier]
    requested_scope = (args.get("energy_scope") or "all").lower()
    if requested_scope not in {
        "all", "tx", "rx", "trx", "full_link", "driver_only", "unknown",
    }:
        requested_scope = "all"
    with engine.connect() as c:
        try:
            rows = c.execute(text(
                "SELECT m.*, i.canonical_paper_id, i.canonical_title, "
                "i.canonical_year, i.canonical_venue, p.article_number, "
                "p.source_system paper_source_system, p.url paper_url, "
                "p.title paper_title, p.authors paper_authors, "
                "p.year paper_year, p.source_name paper_venue, "
                "COALESCE(ec.evidence_count, 0) evidence_count, "
                "ec.process_evidence, a.abstract_text paper_abstract, "
                "family_member.family_id, "
                "COALESCE(subtype_override.link_medium, medium.link_medium, 'unspecified') link_medium, "
                "CASE WHEN subtype_override.paper_id IS NOT NULL THEN subtype_override.override_source ELSE medium.medium_source END medium_source, "
                "CASE WHEN subtype_override.paper_id IS NOT NULL THEN 1.0 ELSE medium.medium_confidence END medium_confidence, "
                "CASE WHEN subtype_override.paper_id IS NOT NULL THEN subtype_override.reason ELSE medium.reason_codes END medium_reason_codes, "
                "CASE WHEN subtype_override.paper_id IS NOT NULL THEN subtype_override.evidence_text ELSE medium.evidence_text END medium_evidence, "
                "COALESCE(subtype_override.link_subtype, subtype.link_subtype, 'mixed_or_unknown') link_subtype, "
                "CASE WHEN subtype_override.paper_id IS NOT NULL THEN subtype_override.override_source ELSE subtype.subtype_source END subtype_source, "
                "CASE WHEN subtype_override.paper_id IS NOT NULL THEN 1.0 ELSE subtype.subtype_confidence END subtype_confidence, "
                "CASE WHEN subtype_override.paper_id IS NOT NULL THEN subtype_override.reason ELSE subtype.reason_codes END subtype_reason_codes, "
                "CASE WHEN subtype_override.paper_id IS NOT NULL THEN subtype_override.evidence_text ELSE subtype.evidence_text END subtype_evidence, "
                "COALESCE(scope_override.energy_component_scope, metric_scope.energy_component_scope, 'unknown') energy_component_scope, "
                "CASE WHEN scope_override.measurement_id IS NOT NULL THEN scope_override.override_source ELSE metric_scope.scope_source END scope_source, "
                "CASE WHEN scope_override.measurement_id IS NOT NULL THEN 1.0 ELSE metric_scope.scope_confidence END scope_confidence, "
                "CASE WHEN scope_override.measurement_id IS NOT NULL THEN scope_override.reason ELSE metric_scope.reason_codes END scope_reason_codes, "
                "CASE WHEN scope_override.measurement_id IS NOT NULL THEN scope_override.evidence_text ELSE metric_scope.evidence_text END scope_evidence, "
                "(SELECT e.source_url FROM serdes_measurement_evidence e "
                " WHERE e.measurement_id=m.id ORDER BY e.id LIMIT 1) evidence_url "
                "FROM serdes_measurements m "
                "JOIN serdes_implementations i ON i.id=m.implementation_id "
                "LEFT JOIN papers p ON p.id=i.canonical_paper_id "
                "LEFT JOIN serdes_paper_screenings screen ON screen.paper_id=p.id "
                f" AND screen.run_id IN ({SERDES_LATEST_COMPLETE_RUNS_SQL}) "
                "LEFT JOIN paper_current_abstracts a ON a.paper_id=p.id AND a.is_current=1 "
                "LEFT JOIN serdes_implementation_family_members family_member "
                "  ON family_member.paper_id=p.id "
                "LEFT JOIN serdes_paper_link_media medium ON medium.paper_id=p.id "
                "LEFT JOIN serdes_paper_link_subtypes subtype ON subtype.paper_id=p.id "
                "LEFT JOIN serdes_paper_subtype_overrides subtype_override ON subtype_override.paper_id=p.id "
                " AND subtype_override.review_status IN ('verified','reviewed','approved','active') "
                "LEFT JOIN serdes_measurement_metric_scopes metric_scope ON metric_scope.measurement_id=m.id "
                "LEFT JOIN serdes_measurement_scope_overrides scope_override ON scope_override.measurement_id=m.id "
                " AND scope_override.review_status IN ('verified','reviewed','approved','active') "
                "LEFT JOIN (SELECT measurement_id, COUNT(*) evidence_count, "
                " GROUP_CONCAT(CASE WHEN field_name='process_nm' THEN evidence_text END "
                "              SEPARATOR ' ') process_evidence "
                "           FROM serdes_measurement_evidence GROUP BY measurement_id) ec "
                "  ON ec.measurement_id=m.id "
                "WHERE (m.lane_rate_gbps IS NOT NULL OR m.aggregate_rate_gbps IS NOT NULL "
                "       OR m.reported_rate_gbps IS NOT NULL OR m.energy_pj_bit IS NOT NULL "
                "       OR m.symbol_rate_gbaud IS NOT NULL OR m.power_mw IS NOT NULL "
                "       OR m.process_nm IS NOT NULL OR m.channel_loss_db IS NOT NULL) "
                "AND m.review_status NOT IN ('rejected','invalid','needs_review') "
                "AND ((screen.paper_id IS NOT NULL AND screen.include_in_survey=1 "
                f"AND {_serdes_source_sql('p')}) "
                "     OR (i.canonical_paper_id IS NULL AND m.source_kind='reference_xlsx')) "
                f"{source_filter}"
            )).mappings().all()
        except SQLAlchemyError:
            if strict:
                raise
            rows = []

    included_rows = [row for row in rows if row.get("canonical_paper_id")]
    unmatched_rows = [row for row in rows if not row.get("canonical_paper_id")]
    has_any_rate = lambda row: any(row.get(field) is not None for field in (
        "lane_rate_gbps", "aggregate_rate_gbps", "reported_rate_gbps",
        "reported_rate_min_gbps", "reported_rate_max_gbps", "symbol_rate_gbaud",
    ))
    has_any_metric = lambda row: any(
        row.get(field) is not None for field in SERDES_INFORMATIVE_FIELDS
    )
    scope_match = lambda row: (
        requested_scope == "all"
        or str(row.get("energy_component_scope") or "unknown") == requested_scope
    )
    selections = {
        "representative": _best_rows_for_implementations(included_rows, has_any_metric),
        "rate": _best_rows_for_implementations(included_rows, has_any_rate),
        "lane_rate": _best_rows_for_implementations(
            included_rows, lambda row: row.get("lane_rate_gbps") is not None,
        ),
        "reported_rate": _best_rows_for_implementations(
            included_rows, lambda row: row.get("reported_rate_gbps") is not None,
        ),
        "aggregate_rate": _best_rows_for_implementations(
            included_rows, lambda row: row.get("aggregate_rate_gbps") is not None,
        ),
        "energy": _best_rows_for_implementations(
            included_rows, lambda row: row.get("energy_pj_bit") is not None and scope_match(row),
            ranker=_energy_measurement_rank,
        ),
        "energy_process": _best_rows_for_implementations(
            included_rows,
            lambda row: row.get("energy_pj_bit") is not None and row.get("process_nm") is not None and scope_match(row),
            ranker=_energy_measurement_rank,
        ),
        "energy_loss": _best_rows_for_implementations(
            included_rows,
            lambda row: row.get("energy_pj_bit") is not None and row.get("channel_loss_db") is not None and scope_match(row),
            ranker=_energy_measurement_rank,
        ),
        "lane_energy": _best_rows_for_implementations(
            included_rows,
            lambda row: row.get("lane_rate_gbps") is not None
            and row.get("energy_pj_bit") is not None and scope_match(row),
            ranker=_energy_measurement_rank,
        ),
        "reported_energy": _best_rows_for_implementations(
            included_rows,
            lambda row: row.get("reported_rate_gbps") is not None
            and row.get("rate_scope") not in (None, "", "unknown")
            and row.get("energy_pj_bit") is not None and scope_match(row),
            ranker=_energy_measurement_rank,
        ),
        "aggregate_energy": _best_rows_for_implementations(
            included_rows,
            lambda row: row.get("aggregate_rate_gbps") is not None
            and row.get("energy_pj_bit") is not None and scope_match(row),
            ranker=_energy_measurement_rank,
        ),
    }
    unmatched_best = _best_rows_for_implementations(
        unmatched_rows,
        lambda row: has_any_metric(row) and (
            requested_scope == "all"
            or (row.get("energy_pj_bit") is not None and scope_match(row))
        ),
    )
    selected_rows = {}
    for selected in selections.values():
        for row in selected.values():
            selected_rows[int(row["id"])] = row

    def point_sort(item):
        return (
            item.get("year") or 0,
            item["performance"].get("lane_rate_gbps") or 0,
            item.get("title") or "",
        )

    points_by_measurement = {
        measurement_id: _performance_point(row)
        for measurement_id, row in selected_rows.items()
    }
    points = sorted(points_by_measurement.values(), key=point_sort)
    unmatched_points = sorted(
        (_performance_point(row) for row in unmatched_best.values()), key=point_sort,
    )
    chart_sets = {
        name: [int(row["id"]) for row in selected.values()]
        for name, selected in selections.items()
    }
    pareto_counts = {}
    rate_fields = {
        "lane": "lane_rate_gbps", "reported": "reported_rate_gbps",
        "aggregate": "aggregate_rate_gbps",
    }
    for mode, rate_field in rate_fields.items():
        pareto_rows = list(selections[f"{mode}_energy"].values())
        group_counts = Counter(
            _pareto_group_key(row, mode)
            for row in pareto_rows
            if row.get("link_medium") in {"electrical", "optical"}
            and row.get("energy_component_scope") not in (None, "", "unknown")
        )
        frontier = pareto_frontier_ids(
            pareto_rows,
            id_getter=lambda row: int(row["id"]),
            x_getter=lambda row, field=rate_field: row.get(field),
            y_getter=lambda row: row.get("energy_pj_bit"),
            group_getter=lambda row, chart_mode=mode: (
                _pareto_group_key(row, chart_mode)
                if group_counts.get(_pareto_group_key(row, chart_mode), 0) >= 3
                else None
            ),
        )
        chart_sets[f"pareto_{mode}"] = sorted(frontier)
        pareto_counts[mode] = len(frontier)
    representative_points = [
        points_by_measurement[measurement_id]
        for measurement_id in chart_sets["representative"]
    ]
    source_counts = Counter()
    medium_counts = Counter()
    for item in representative_points:
        source_counts[item["performance"]["source_kind"]] += 1
        medium_counts[item.get("link_medium") or "unspecified"] += 1
    years = [item["year"] for item in representative_points if item.get("year")]
    energy_points = [
        points_by_measurement[int(row["id"])]
        for row in selections["energy"].values()
    ]
    coverage = dict(
        total=len(representative_points),
        any_rate_total=len(chart_sets["rate"]),
        rate_total=len(chart_sets["lane_rate"]),
        reported_rate_total=len(chart_sets["reported_rate"]),
        aggregate_rate_total=len(chart_sets["aggregate_rate"]),
        energy_total=len(chart_sets["energy"]),
        reported_energy_total=sum(
            item["performance"].get("energy_basis") != FOM_ENERGY_BASIS
            for item in energy_points
        ),
        calculated_energy_total=sum(
            item["performance"].get("energy_basis") == FOM_ENERGY_BASIS
            for item in energy_points
        ),
        process_energy_total=len(chart_sets["energy_process"]),
        loss_energy_total=len(chart_sets["energy_loss"]),
        pareto=pareto_counts,
        unmatched_total=len(unmatched_points),
        year_min=min(years) if years else None,
        year_max=max(years) if years else None,
        sources=[
            dict(kind=kind, label=SERDES_SOURCE_LABELS.get(kind, kind), count=count)
            for kind, count in source_counts.most_common()
        ],
        link_media=[
            dict(medium=medium, count=int(medium_counts.get(medium, 0)))
            for medium in ("electrical", "optical", "unspecified")
        ],
    )
    return dict(
        points=points,
        chart_sets=chart_sets,
        unmatched_points=unmatched_points,
        coverage=coverage,
        evidence_tier=requested_tier,
        energy_scope=requested_scope,
    )


@app.route("/api/serdes/papers/<article_number>/evidence")
def api_serdes_evidence(article_number):
    if not SAFE_KEY_RE.fullmatch(article_number):
        abort(404)
    with engine.connect() as c:
        paper = c.execute(text(
            "SELECT id, article_number, title, url, source_system FROM papers "
            f"WHERE article_number=:article_number AND {_serdes_source_sql()}"
        ), {"article_number": article_number}).mappings().first()
        if not paper:
            abort(404)
        try:
            rows = c.execute(text(
                "SELECT e.field_name, e.source_kind, e.source_url, e.source_locator, "
                "e.evidence_text, e.extraction_method, e.extractor_version, "
                "e.confidence, e.review_status, e.updated_at, m.id measurement_id, "
                "m.operating_point_key "
                "FROM serdes_implementation_papers ip "
                "JOIN serdes_measurements m ON m.implementation_id=ip.implementation_id "
                "JOIN serdes_measurement_evidence e ON e.measurement_id=m.id "
                "WHERE ip.paper_id=:paper_id "
                "ORDER BY CASE e.source_kind "
                " WHEN 'manual' THEN 6 WHEN 'user_sheet' THEN 5 WHEN 'pdf' THEN 4 "
                " WHEN 'reference_xlsx' THEN 3 WHEN 'abstract' THEN 2 ELSE 1 END DESC, "
                "e.field_name, e.updated_at DESC LIMIT 80"
            ), {"paper_id": paper["id"]}).mappings().all()
        except SQLAlchemyError:
            rows = []
    evidence = []
    seen = set()
    for row in rows:
        key = (row["measurement_id"], row["field_name"], row["evidence_text"])
        if key in seen:
            continue
        seen.add(key)
        evidence.append(dict(
            field=row["field_name"],
            source_kind=row["source_kind"],
            source_label=SERDES_SOURCE_LABELS.get(row["source_kind"], row["source_kind"]),
            source_url=row["source_url"],
            locator=row["source_locator"],
            text=row["evidence_text"],
            extraction_method=row["extraction_method"],
            extractor_version=row["extractor_version"],
            confidence=_json_scalar(row["confidence"]),
            review_status=row["review_status"],
            updated_at=_json_scalar(row["updated_at"]),
        ))
    return jsonify(dict(
        article_number=article_number,
        title=paper["title"],
        url=_serdes_paper_url(article_number, paper.get("source_system"), paper.get("url")),
        evidence=evidence,
    ))


@app.route("/api/meta")
def api_meta():
    with engine.connect() as c:
        years = [r[0] for r in c.execute(text(
            "SELECT DISTINCT year FROM papers WHERE year IS NOT NULL "
            "ORDER BY year DESC"))]
        sources = [dict(name=r[0], type=r[1], count=r[2]) for r in c.execute(text(
            "SELECT source_name, source_type, COUNT(*) c FROM papers "
            "WHERE source_name IS NOT NULL GROUP BY source_name, source_type "
            "ORDER BY c DESC"))]
        pub_counts = dict(c.execute(text(
            "SELECT source_system, COUNT(*) FROM papers GROUP BY source_system")).all())
        publishers = [dict(p, count=pub_counts.get(p["code"], 0)) for p in PUBLISHERS]
        total = c.execute(text("SELECT COUNT(*) FROM papers")).scalar()
        counts = c.execute(text(
            "SELECT "
            "COUNT(CASE WHEN pdf_available=1 THEN 1 END) AS pdf_total, "
            f"COUNT(CASE WHEN {favorite_sql()}=1 THEN 1 END) AS fav_total, "
            f"COUNT(CASE WHEN {favorite_sql()}=1 AND pdf_available=1 THEN 1 END) AS fav_pdf_total, "
            f"COUNT(CASE WHEN {favorite_sql()}=1 AND COALESCE(pdf_available,0)=0 THEN 1 END) AS fav_missing_pdf_total, "
            f"COUNT(CASE WHEN COALESCE({favorite_sql()},0)=0 AND pdf_available=1 THEN 1 END) AS nonfav_pdf_total "
            "FROM papers"
        )).mappings().one()
    return jsonify(dict(
        years=years, sources=sources, publishers=publishers,
        total=total, **dict(counts),
        year_min=(min(years) if years else None),
        year_max=(max(years) if years else None),
    ))


@app.route("/api/papers")
def api_papers():
    q = (request.args.get("q") or "").strip()
    year = (request.args.get("year") or "").strip()
    source = (request.args.get("source") or "").strip()
    ptype = (request.args.get("type") or "").strip()
    publisher = (request.args.get("publisher") or "").strip()
    pdf_status = request.args.get("pdf_status", "")
    fav_status = request.args.get("fav_status", "")
    if not pdf_status and request.args.get("pdf_only") in ("1", "true", "on", "yes"):
        pdf_status = "present"
    if not fav_status and request.args.get("fav_only") in ("1", "true", "on", "yes"):
        fav_status = "favorite"
    if pdf_status not in ("", "present", "missing") or fav_status not in ("", "favorite", "nonfavorite"):
        return jsonify(error="잘못된 PDF 또는 즐겨찾기 필터입니다."), 400
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        size = min(200, max(1, int(request.args.get("size", 50))))
    except ValueError:
        size = 50

    where = ["1=1"]
    params = {}
    if q:
        like = f"%{esc_like(q)}%"
        where.append("(title LIKE :like ESCAPE '\\\\' OR authors LIKE :like ESCAPE '\\\\')")
        params["like"] = like
    if year:
        where.append("year = :year")
        params["year"] = year
    if source:
        where.append("source_name = :source")
        params["source"] = source
    if ptype:
        where.append("source_type = :ptype")
        params["ptype"] = ptype
    if publisher:
        where.append("source_system = :publisher")
        params["publisher"] = publisher
    if pdf_status:
        where.append("COALESCE(pdf_available, 0) = :pdf_status")
        params["pdf_status"] = int(pdf_status == "present")
    if fav_status:
        where.append(f"COALESCE({favorite_sql()}, 0) = :fav_status")
        params["fav_status"] = int(fav_status == "favorite")

    where_sql = " AND ".join(where)
    offset = (page - 1) * size

    with engine.connect() as c:
        total = c.execute(
            text(f"SELECT COUNT(*) FROM papers WHERE {where_sql}"), params
        ).scalar()
        rows = c.execute(text(
            f"SELECT id, article_number, title, authors, year, source_name, "
            f"source_type, source_system, issue, url, pdf_available, {favorite_sql()} AS is_favorite, "
            f"citation_count, citation_source, citation_updated_at "
            f"FROM papers WHERE {where_sql} "
            f"ORDER BY (year+0) DESC, id DESC "
            f"LIMIT :size OFFSET :offset"),
            {**params, "size": size, "offset": offset},
        ).mappings().all()

    items = [dict(r) for r in rows]
    pages = (total + size - 1) // size if total else 0
    return jsonify(dict(
        items=items, total=total, page=page, size=size, pages=pages))


RECO_STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or", "with",
    "using", "based", "via", "from", "into", "towards", "toward", "by", "is",
    "are", "be", "this", "that", "these", "those", "its", "their", "new",
    "novel", "high", "low", "performance", "design", "analysis", "study",
    "approach", "method", "system", "systems", "model", "models", "paper",
    "review", "under", "over", "between", "as", "we", "propose", "proposed",
    "present", "presented", "results", "result", "effect", "effects", "case",
    "characterization", "fabrication", "implementation", "application",
    "applications", "toward", "improved", "improving", "efficient",
    "publisher", "author", "correction", "editorial", "erratum", "per",
}
RECO_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*")
RECO_DASH_RE = re.compile(r"[\u2010-\u2015\u2212]")
RECO_TITLE_PREFIX_RE = re.compile(
    r"^\s*(?:(?:publisher|author)\s+correction|correction(?:\s+to)?|"
    r"erratum|addendum|retraction\s+note|expression\s+of\s+concern)\s*[:\-]?\s*",
    re.IGNORECASE,
)
RECO_LOW_QUALITY_PREFIXES = (
    "publisher correction", "author correction", "correction:", "correction to",
    "erratum", "addendum", "retraction note", "expression of concern", "editorial:",
)
RECO_TERM_ALIASES = {
    "pam-2": "pam", "pam-3": "pam", "pam-4": "pam",
    "pam2": "pam", "pam3": "pam", "pam4": "pam",
}
RECO_PROFILE_TOPN = 40
RECO_PER_SOURCE = 20
RECO_MAX_PER_SOURCE = 20
RECO_LOCAL_PRIOR = 40
RECO_FIXED_PER_SOURCE = 3
RECO_ROTATION_POOL_SIZE = 60
RECO_TIMEZONE = ZoneInfo("Asia/Seoul")


def _title_terms(title):
    """제목을 MySQL FULLTEXT 검색에 맞는 결정적 키워드 집합으로 정규화한다."""
    normalized = RECO_DASH_RE.sub("-", title or "")
    terms = set()
    for raw in RECO_WORD_RE.findall(normalized):
        word = RECO_TERM_ALIASES.get(raw.lower().strip("-"), raw.lower().strip("-"))
        if len(word) < 3 or word in RECO_STOPWORDS:
            continue
        terms.add(word)
    return terms


def _build_profile(titles, topn=RECO_PROFILE_TOPN, min_freq=1):
    """제목 목록에서 반복되는 핵심 키워드를 빈도/알파벳순으로 안정적으로 뽑는다."""
    freq = Counter()
    for title in titles:
        freq.update(_title_terms(title))
    ranked = sorted(
        ((word, count) for word, count in freq.items() if count >= min_freq),
        key=lambda item: (-item[1], item[0]),
    )
    return [word for word, _ in ranked[:topn]]


def _local_profile_weight(favorite_count):
    """표본이 적은 로컬 프로필의 영향은 낮추고, 충분하면 최대 65%까지 반영한다."""
    if favorite_count <= 0:
        return 0.0
    return min(0.65, favorite_count / (favorite_count + RECO_LOCAL_PRIOR))


def _merge_profile_terms(global_terms, local_terms, local_weight, topn=RECO_PROFILE_TOPN):
    """화면 표시와 커버리지 계산용으로 글로벌/로컬 키워드를 결정적으로 합친다."""
    primary, secondary = (
        (local_terms, global_terms) if local_weight >= 0.5 else (global_terms, local_terms)
    )
    merged, seen = [], set()
    for group in zip_longest(primary, secondary):
        for word in group:
            if word and word not in seen:
                seen.add(word)
                merged.append(word)
                if len(merged) >= topn:
                    return merged
    return merged


def _title_key(title):
    """교정문 접두사와 문장부호를 제거한 제목 비교 키."""
    normalized = RECO_DASH_RE.sub("-", title or "").strip().lower()
    normalized = RECO_TITLE_PREFIX_RE.sub("", normalized)
    return re.sub(r"[^a-z0-9]+", " ", normalized).strip()


def _is_low_quality_title(title):
    normalized = (title or "").strip().lower()
    return normalized.startswith(RECO_LOW_QUALITY_PREFIXES) or bool(re.search(r":\s*publisher[’']s note\s*$", normalized))


def _year_number(value):
    match = re.match(r"^(\d{4})", str(value or ""))
    return int(match.group(1)) if match else 0


def _rank_and_filter_candidates(rows, profile_terms, favorite_title_keys, limit, current_year=None,
                                similarity=None):
    """교정문/중복을 제거하고 키워드 커버리지·최신성으로 재랭킹한다."""
    current_year = current_year or date.today().year
    ranked, seen_titles = [], set()
    profile_set = set(profile_terms)

    for raw in rows:
        item = dict(raw)
        title = item.get("title") or ""
        if excluded_electrical_multicarrier(item):
            continue
        title_key = _title_key(title)
        if not title_key or _is_low_quality_title(title):
            continue
        if title_key in favorite_title_keys or title_key in seen_titles:
            continue
        seen_titles.add(title_key)

        overlap = len(_title_terms(title) & profile_set)
        if overlap < min(3, len(profile_set)):
            continue
        if similarity is not None:
            item.update(similarity.evidence(title))
            if item['favorite_similarity'] < MIN_SIMILARITY or item['near_duplicate_favorite']:
                continue
        published_year = _year_number(item.get("year"))
        age = max(0, current_year - published_year) if published_year else 10
        recency_factor = max(0.85, 1.12 - 0.02 * age)
        coverage_factor = 1.0 + 0.06 * min(overlap, 6)
        text_score = float(item.get("score") or 0.0)
        item["text_score"] = text_score
        item["keyword_overlap"] = overlap
        item["score"] = text_score * recency_factor * coverage_factor
        ranked.append(item)

    ranked.sort(
        key=lambda item: (
            -item.get('favorite_similarity', 0), -item["score"],
            -_year_number(item.get("year")), item.get("title") or ""
        )
    )

    return ranked[:limit]


def _select_daily_recommendations(
    ranked,
    source_name,
    limit,
    rotation_date,
    fixed_count=RECO_FIXED_PER_SOURCE,
    rotation_pool_size=RECO_ROTATION_POOL_SIZE,
):
    """Keep the strongest papers fixed and rotate the rest once per Seoul day.

    Rotation happens only inside the already title-ranked top candidate pool;
    it never changes the profile, FULLTEXT score, quality filters, or favorite
    exclusions.  A source-specific phase prevents every venue from traversing
    the same rank window on the same day.
    """
    limit = max(0, int(limit))
    if not ranked or limit <= 0:
        return []

    eligible = list(ranked[:max(limit, rotation_pool_size)])
    anchor_count = min(max(0, int(fixed_count)), limit, len(eligible))
    anchors = eligible[:anchor_count]
    rotating_slots = min(limit - anchor_count, len(eligible) - anchor_count)
    if rotating_slots <= 0:
        return anchors

    rotation_pool = eligible[anchor_count:]
    if len(rotation_pool) <= rotating_slots:
        return anchors + rotation_pool

    phase = int.from_bytes(
        hashlib.blake2s(str(source_name).encode("utf-8"), digest_size=4).digest(),
        "big",
    )
    start = (
        phase + rotation_date.toordinal() * rotating_slots
    ) % len(rotation_pool)
    indices = sorted(
        (start + offset) % len(rotation_pool)
        for offset in range(rotating_slots)
    )
    return anchors + [rotation_pool[index] for index in indices]


def _search_source(
    c, source_name, global_terms, local_terms, sub_limit, local_weight,
    favorite_title_keys, rotation_date, candidate_pool=False, similarity=None, feedback_available=False,
):
    """글로벌+저널/학회 로컬 프로필로 한 출처의 후보를 뽑아 재랭킹한다."""
    if not global_terms or sub_limit <= 0:
        return []

    global_query = " ".join(global_terms)
    local_query = " ".join(local_terms)
    effective_terms = _merge_profile_terms(global_terms, local_terms, local_weight)
    pool_limit = min(500, max(50, sub_limit * 8))

    params = {
        "global_q": global_query,
        "local_q": local_query,
        "global_weight": 1.0 - local_weight,
        "local_weight": local_weight,
        "pool_limit": pool_limit,
        "source_name": source_name,
    }
    if local_query and local_weight > 0:
        score_sql = (
            "(:global_weight * MATCH(title, authors) "
            "AGAINST(:global_q IN NATURAL LANGUAGE MODE) + "
            ":local_weight * MATCH(title, authors) "
            "AGAINST(:local_q IN NATURAL LANGUAGE MODE))"
        )
        match_sql = (
            "(MATCH(title, authors) AGAINST(:global_q IN NATURAL LANGUAGE MODE) > 0 "
            "OR MATCH(title, authors) AGAINST(:local_q IN NATURAL LANGUAGE MODE) > 0)"
        )
    else:
        score_sql = "MATCH(title, authors) AGAINST(:global_q IN NATURAL LANGUAGE MODE)"
        match_sql = score_sql + " > 0"

    feedback_sql = ("AND NOT EXISTS (SELECT 1 FROM recommendation_feedback rf "
                    "WHERE rf.article_number=papers.article_number) ") if feedback_available else ""
    if feedback_available and current_account_id.get() is not None:
        feedback_sql = ('AND NOT EXISTS (SELECT 1 FROM account_recommendation_feedback rf '
                        'WHERE rf.account_id=:feedback_account AND rf.article_number=papers.article_number) ')
        params['feedback_account'] = current_account_id.get()
    rows = c.execute(text(
        f"SELECT id, article_number, title, authors, year, source_name, "
        f"source_type, source_system, issue, url, pdf_available, {favorite_sql()} AS is_favorite, "
        f"{score_sql} AS score "
        f"FROM papers "
        f"WHERE {favorite_sql()} = 0 "
        f"AND source_name = :source_name "
        f"{feedback_sql}"
        f"AND {match_sql} "
        f"AND LOWER(title) NOT LIKE 'publisher correction:%' "
        f"AND LOWER(title) NOT LIKE 'author correction:%' "
        f"AND LOWER(title) NOT LIKE 'correction:%' "
        f"AND LOWER(title) NOT LIKE 'correction to:%' "
        f"ORDER BY score DESC "
        f"LIMIT :pool_limit"),
        params,
    ).mappings().all()
    ranked = _rank_and_filter_candidates(
        rows,
        effective_terms,
        favorite_title_keys,
        RECO_ROTATION_POOL_SIZE,
        current_year=rotation_date.year,
        similarity=similarity,
    )
    if candidate_pool:
        return ranked[:sub_limit]
    return ranked[:sub_limit]


def _assemble_source_recommendations(
    source_specs, items_by_source, effective_profiles, favorite_counts, per_source,
):
    """출처 순서를 보존하면서 출처별 최대 per_source건과 그룹 메타를 조립한다."""
    items, groups = [], []
    for source in source_specs:
        name = source["name"]
        group_items = [dict(item) for item in items_by_source.get(name, [])[:per_source]]
        if not group_items:
            continue
        for item in group_items:
            item["source_group"] = item.get("source_system")
        items.extend(group_items)
        groups.append({
            "name": name,
            "type": source["type"],
            "count": len(group_items),
            "fav_count": favorite_counts.get(name, 0),
            "profile_terms": effective_profiles.get(name, [])[:15],
        })
    return items, groups


def build_sql_recommendations(per_source=RECO_PER_SOURCE, candidate_pool=False):
    """출처 영향 제한 + 개별 즐겨찾기 유사도로 전체 최대 180편을 고른다.

    출처당 20편은 상한이며 최소 할당은 없다. candidate_pool은 AI 평가용
    넓은 후보를 반환하지만 관련성·중복·명시적 제외 기준은 동일하다.
    """
    rotation_date = datetime.now(RECO_TIMEZONE).date()
    from recommendation_feedback import FeedbackStore
    feedback = FeedbackStore(engine).read()

    with engine.connect() as c:
        fav_rows = c.execute(text(
            "SELECT source_name, title, article_number FROM papers "
            f"WHERE {favorite_sql()}=1 AND title IS NOT NULL"
        )).mappings().all()
        source_rows = c.execute(text(
            "SELECT source_name, source_type FROM papers "
            "WHERE source_name IS NOT NULL "
            "GROUP BY source_name, source_type "
            "ORDER BY CASE WHEN source_type='journal' THEN 0 ELSE 1 END, source_name"
        )).mappings().all()

    fav_count = len(fav_rows)
    if not fav_count:
        return dict(items=[], groups=[], profile_terms=[], fav_count=0)

    source_specs = [
        {"name": row["source_name"], "type": row["source_type"]}
        for row in source_rows
    ]
    titles_by_source = {source["name"]: [] for source in source_specs}
    for row in fav_rows:
        if excluded_electrical_multicarrier(row):
            continue
        source_name, title = row['source_name'], row['title']
        if source_name in titles_by_source:
            titles_by_source[source_name].append(title)

    all_titles = [row['title'] for row in fav_rows]
    favorite_topic_counts = Counter(
        classify_interest_topic(row) for row in fav_rows
        if not excluded_electrical_multicarrier(row)
    )
    global_profile = weighted_profile(fav_rows, _title_terms)
    similarity = FavoriteSimilarity(fav_rows, _title_terms)
    if not global_profile:
        return dict(items=[], groups=[], profile_terms=[], fav_count=fav_count)

    counts = {name: len(titles) for name, titles in titles_by_source.items()}
    local_profiles = {}
    local_weights = {}
    effective_profiles = {}
    for source in source_specs:
        name = source["name"]
        local_min_freq = 2 if counts[name] >= 5 else 1
        local_profiles[name] = _build_profile(
            titles_by_source[name], min_freq=local_min_freq,
        )
        local_weights[name] = (
            _local_profile_weight(counts[name]) if local_profiles[name] else 0.0
        )
        effective_profiles[name] = _merge_profile_terms(
            global_profile, local_profiles[name], local_weights[name],
        )

    favorite_title_keys = {_title_key(title) for title in all_titles if _title_key(title)}

    with engine.connect() as c:
        items_by_source = {
            source["name"]: _search_source(
                c,
                source["name"],
                global_profile,
                local_profiles[source["name"]],
                per_source,
                local_weights[source["name"]],
                favorite_title_keys,
                rotation_date,
                candidate_pool=candidate_pool,
                similarity=similarity,
                feedback_available=feedback['available'],
            )
            for source in source_specs
        }

    items, groups = _assemble_source_recommendations(
        source_specs, items_by_source, effective_profiles, counts, per_source,
    )
    items = [p for p in items if str(p['article_number']) not in feedback['actions']]
    if not candidate_pool:
        items = allocate(items, per_source=per_source)
    counts_selected = Counter(p['source_name'] for p in items)
    groups = [{**g, 'count': counts_selected[g['name']]} for g in groups if counts_selected[g['name']]]
    source_order = {p['source_name']: i for i, p in reversed(list(enumerate(items)))}
    groups.sort(key=lambda g: source_order[g['name']])

    result = dict(
        items=items,
        groups=groups,
        profile_terms=global_profile[:15],
        favorite_topic_counts=dict(favorite_topic_counts),
        fav_count=fav_count,
        per_source=per_source,
        total_limit=TOTAL_RECOMMENDATIONS,
        fixed_per_source=0,
        rotating_per_source=0,
        rotation_date=rotation_date.isoformat(),
        source_count=len(groups),
        total_source_count=len(source_specs),
        allocation={group["name"]: group["count"] for group in groups},
    )
    return result


from local_ai_recommendations import RecommendationService  # noqa: E402
from serdes_figures import FigureStore  # noqa: E402

serdes_figures = FigureStore(ROOT)


def _diagram_paper(article_number):
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,180}', article_number):
        abort(404)
    with engine.connect() as conn:
        paper = conn.execute(text(
            'SELECT id,article_number,title,source_system,pdf_local_path,pdf_available '
            f'FROM papers WHERE article_number=:article AND {_serdes_source_sql()}'), {'article': article_number}).mappings().first()
    if paper is None:
        abort(404)
    return dict(paper)


@app.route('/api/serdes/papers/<article_number>/block-diagram')
def api_serdes_block_diagram(article_number):
    # Authenticated reads only. No parser/model work or DB mutations on hover.
    paper = _diagram_paper(article_number)
    measurement = None
    measurement_id = request.args.get('measurement_id')
    if measurement_id is not None:
        if not re.fullmatch(r'[1-9]\d{0,18}', measurement_id):
            abort(400)
        with engine.connect() as conn:
            row = conn.execute(text(
                'SELECT m.id,m.implementation_id,m.component_scope FROM serdes_measurements m '
                'JOIN serdes_implementations i ON i.id=m.implementation_id '
                'WHERE m.id=:measurement AND i.canonical_paper_id=:paper'),
                {'measurement':int(measurement_id), 'paper':paper['id']}).mappings().first()
        if row is None:
            abort(404)
        measurement = dict(row)
    result = serdes_figures.public(paper, measurement=measurement)
    response = jsonify(ok=True, **result)
    response.headers['Cache-Control'] = 'private, no-store'
    return response


@app.route('/api/serdes/papers/<article_number>/block-diagram/image')
def api_serdes_block_diagram_image(article_number):
    path = serdes_figures.image_path(_diagram_paper(article_number))
    if path is None:
        abort(404)
    # A stale image URL must not silently resolve to a different crop after rebuild.
    if request.args.get('v') and request.args['v'] != path.stem:
        abort(404)
    response = send_file(path, mimetype='image/png', conditional=True, max_age=0)
    response.headers['Cache-Control'] = 'private, no-cache'
    return response

def build_sql_recommendation_pool(per_source):
    """Broad deterministic pool; final mode selection applies the visible limits."""
    return build_sql_recommendations(max(30, per_source), candidate_pool=True)


# Favorites recommendations are deliberately SQL-only. Research Desk and Survey
# AI remain available through the separate local_ai blueprint.
sql_recommendations = RecommendationService(engine, None, build_sql_recommendation_pool)
_account_recommendations = {}
_account_recommendations_lock = threading.Lock()


def personal_recommendations():
    account_id = current_account_id.get()
    if account_id is None:
        return sql_recommendations
    with _account_recommendations_lock:
        if account_id not in _account_recommendations:
            _account_recommendations[account_id] = RecommendationService(
                engine, None, build_sql_recommendation_pool, account_id=account_id)
        return _account_recommendations[account_id]


@app.route("/api/recommendations")
def api_recommendations():
    from local_ai_recommendations import MODES
    mode = request.args.get("mode", "match")
    if mode not in MODES:
        return jsonify(ok=False, error="지원하지 않는 추천 모드입니다."), 400
    try:
        per_source = min(RECO_MAX_PER_SOURCE, max(1, int(request.args.get("per_source", RECO_PER_SOURCE))))
    except ValueError:
        per_source = RECO_PER_SOURCE
    try:
        result = personal_recommendations().view(mode, per_source)
    except (SQLAlchemyError, RuntimeError, ValueError):
        return jsonify(ok=False, error="추천을 조회하지 못했습니다. 잠시 후 다시 시도하세요."), 503
    response = jsonify(result)
    response.headers['Cache-Control'] = 'private, no-store'
    return response


@app.route("/api/recommendations/refresh", methods=["POST"])
def refresh_recommendations():
    if session.get("role") != "admin":
        return jsonify(ok=False, error="관리자만 추천을 다시 생성할 수 있습니다."), 403
    return jsonify(ok=False, status="disabled",
                   error="즐겨찾기 추천은 규칙 기반으로 전환되어 AI 갱신을 사용하지 않습니다."), 410


@app.route('/api/recommendations/feedback', methods=['GET', 'POST'])
def recommendation_feedback():
    from recommendation_feedback import FeedbackStore
    store = FeedbackStore(engine)
    if request.method == 'GET':
        state = store.read()
        items = list(state['actions'].values())
        if items:
            stmt = text('SELECT article_number,title FROM papers WHERE article_number IN :ids').bindparams(bindparam('ids', expanding=True))
            with engine.connect() as conn:
                titles = {str(r.article_number): r.title for r in conn.execute(stmt, {'ids': list(state['actions'])})}
            items = [{**r, 'title': titles.get(r['article_number'], r['article_number'])} for r in items]
            items.sort(key=lambda r: (str(r['updated_at']), r['article_number']), reverse=True)
        return jsonify(available=state['available'], items=items)
    if session.get('role') not in {'admin', 'member'}:
        return jsonify(ok=False, error='administrator required'), 403
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get('article_number'), str):
        return jsonify(ok=False, error='article_number required'), 400
    article = data['article_number'].strip()
    if not article or len(article) > 255:
        return jsonify(ok=False, error='invalid article_number'), 400
    try:
        if not store.read()['available']:
            return jsonify(ok=False, error='추천 기록 기능이 아직 준비되지 않았습니다.'), 503
        store.save(article, data.get('action'))
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    except KeyError:
        return jsonify(ok=False, error='not found'), 404
    return jsonify(ok=True, article_number=article, action=data['action'])


@app.route("/api/favorite", methods=["POST"])
def api_favorite():
    """즐겨찾기 토글/설정. body: {article_number, favorite?}
    favorite 미지정 시 현재값 반전(토글)."""
    if session.get("role") not in {"admin", "member"}:
        return jsonify(dict(ok=False, error="administrator required")), 403
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(ok=False, error="JSON object required"), 400
    art = str(data.get("article_number") or "").strip()
    if not art:
        return jsonify(dict(ok=False, error="article_number required")), 400
    if current_account_id.get() is not None:
        value = data.get('favorite') in (True, 1, '1', 'true', 'on') if 'favorite' in data else None
        try:
            result = accounts.set_favorites(current_account_id.get(), [art], value)
        except KeyError:
            return jsonify(ok=False, error='not found'), 404
        except PermissionError:
            return jsonify(ok=False, error='personal settings permission required'), 403
        return jsonify(ok=True, article_number=art, **result)
    with engine.begin() as c:
        previous = c.execute(text(
            "SELECT is_favorite FROM papers WHERE article_number=:a FOR UPDATE"
        ), {"a": art}).scalar()
        if previous is None:
            return jsonify(ok=False, error="not found"), 404
        if "favorite" in data:
            newval = 1 if data.get("favorite") in (True, 1, "1", "true", "on") else 0
            c.execute(text("UPDATE papers SET is_favorite=:v WHERE article_number=:a"),
                      {"v": newval, "a": art})
        else:
            c.execute(text("UPDATE papers SET is_favorite = 1 - is_favorite "
                           "WHERE article_number=:a"), {"a": art})
        cur = c.execute(text("SELECT is_favorite FROM papers WHERE article_number=:a"),
                        {"a": art}).scalar()
        if cur == 0:
            c.execute(text(
                "UPDATE papers SET citation_count=NULL, citation_source=NULL, "
                "citation_updated_at=NULL WHERE article_number=:a"
            ), {"a": art})
        citation_count = c.execute(text(
            "SELECT citation_count FROM papers WHERE article_number=:a"
        ), {"a": art}).scalar()
    if cur is None:
        return jsonify(dict(ok=False, error="not found")), 404
    return jsonify(dict(
        ok=True,
        article_number=art,
        is_favorite=int(cur),
        citation_count=citation_count,
        changed_count=int(cur != previous),
        favorite_delta=int(cur) - int(previous),
    ))


@app.route("/api/favorites/bulk", methods=["POST"])
def api_favorites_bulk():
    """Idempotently add the displayed recommendations in one atomic transaction."""
    if session.get("role") not in {"admin", "member"}:
        return jsonify(ok=False, error="administrator required"), 403
    data = request.get_json(silent=True)
    articles = data.get("article_numbers") if isinstance(data, dict) else None
    if (not isinstance(articles, list) or not 1 <= len(articles) <= 1000
            or any(not isinstance(a, str) or not a.strip() or len(a) > 255 for a in articles)):
        return jsonify(ok=False, error="article_numbers must contain 1–1000 non-empty strings"), 400
    articles = sorted(set(a.strip() for a in articles))
    if current_account_id.get() is not None:
        try:
            result = accounts.set_favorites(current_account_id.get(), articles, True)
        except KeyError:
            return jsonify(ok=False, error='일부 논문을 찾을 수 없어 전체 추가를 취소했습니다.'), 404
        except PermissionError:
            return jsonify(ok=False, error='personal settings permission required'), 403
        return jsonify(ok=True, article_numbers=articles, **result)
    select = text("SELECT article_number,is_favorite FROM papers "
                  "WHERE article_number IN :articles ORDER BY article_number FOR UPDATE").bindparams(
                      bindparam("articles", expanding=True))
    with engine.begin() as c:
        rows = list(c.execute(select, {"articles": articles}).mappings())
        if len(rows) != len(articles):
            return jsonify(ok=False, error="일부 논문을 찾을 수 없어 전체 추가를 취소했습니다."), 404
        changed = [row['article_number'] for row in rows if not row['is_favorite']]
        if changed:
            c.execute(text("UPDATE papers SET is_favorite=1 WHERE article_number IN :articles").bindparams(
                bindparam("articles", expanding=True)), {"articles": changed})
    return jsonify(ok=True, article_numbers=articles, changed_count=len(changed),
                   favorite_delta=len(changed), is_favorite=1)


@app.route("/pdf/<article_number>")
def serve_pdf(article_number):
    # 보안: 경로 조작(../, 슬래시 등) 차단 — 폴더 선택은 DB의 source_system이 기준
    if not SAFE_KEY_RE.match(article_number):
        abort(404)
    with engine.connect() as c:
        row = c.execute(text(
            "SELECT source_system, pdf_local_path "
            "FROM papers WHERE article_number=:a"),
            {"a": article_number}).mappings().first()
    if not row:
        abort(404)
    path = _resolve_pdf_path(
        row["source_system"], article_number, row["pdf_local_path"]
    )
    if not path:
        abort(404)
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype="application/pdf")


from web.local_ai_api import create_ai_blueprint  # noqa: E402

app.register_blueprint(create_ai_blueprint(engine, ROOT, _csrf_token))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
