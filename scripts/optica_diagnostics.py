# -*- coding: utf-8 -*-
"""Local-only diagnostics for an already-open Optica browser page.

The capture routine never navigates, clicks, downloads, reads cookies, or saves
raw HTML.  It records only sanitized routing facts and page-state markers so a
failed scheduled run cannot amplify publisher traffic.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from selenium.webdriver.common.by import By


ROOT = Path(__file__).resolve().parent.parent
DIAGNOSTIC_DIR = ROOT / "logs" / "pdf_download_state" / "optica_diagnostics"
SAFE_QUERY_KEYS = {"uri", "seq"}


def sanitize_url(value: str | None) -> str | None:
    """Preserve publisher routing while redacting tokens and session values."""
    candidate = str(value or "").strip()
    if not candidate:
        return None
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return "<invalid-url>"
    sanitized_query = []
    for key, item_value in parse_qsl(parsed.query, keep_blank_values=True):
        sanitized_query.append(
            (key, item_value if key.lower() in SAFE_QUERY_KEYS else "<redacted>")
        )
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(sanitized_query),
            "",
        )
    )


def _safe_driver_value(callback, default=""):
    try:
        return callback()
    except Exception:
        return default


def _link_candidates(driver) -> list[dict[str, str | None]]:
    candidates: list[dict[str, str | None]] = []
    elements = _safe_driver_value(
        lambda: driver.find_elements(By.CSS_SELECTOR, "a[href]"), []
    )
    for element in elements:
        text = str(_safe_driver_value(lambda: element.text, "") or "").strip()
        href = str(
            _safe_driver_value(lambda: element.get_attribute("href"), "") or ""
        ).strip()
        searchable = f"{text} {href}".lower()
        if not any(marker in searchable for marker in ("pdf", "viewmedia", "directpdf")):
            continue
        candidates.append(
            {
                "text": text[:160],
                "href": sanitize_url(href),
            }
        )
        if len(candidates) >= 20:
            break
    return candidates


def _embedded_resource_candidates(driver) -> list[dict[str, str | None]]:
    candidates: list[dict[str, str | None]] = []
    for selector, attribute in (
        ("iframe[src]", "src"),
        ("embed[src]", "src"),
        ("object[data]", "data"),
    ):
        elements = _safe_driver_value(
            lambda selector=selector: driver.find_elements(By.CSS_SELECTOR, selector),
            [],
        )
        for element in elements:
            value = str(
                _safe_driver_value(
                    lambda element=element, attribute=attribute: element.get_attribute(
                        attribute
                    ),
                    "",
                )
                or ""
            ).strip()
            if not value:
                continue
            candidates.append(
                {
                    "element": selector.split("[", 1)[0],
                    "url": sanitize_url(value),
                }
            )
            if len(candidates) >= 20:
                return candidates
    return candidates


def capture_optica_diagnostic(
    driver,
    *,
    article_number: str,
    stored_url: str | None,
    resolved_url: str,
    fallback_url: str | None,
    failure_reason: str,
    failure_details: dict | None = None,
    trigger: str = "three_consecutive_failures",
) -> Path:
    """Capture the current DOM state without causing any new network request."""
    DIAGNOSTIC_DIR.mkdir(parents=True, exist_ok=True)
    safe_key = re.sub(r"[^A-Za-z0-9._-]+", "_", str(article_number))[:120]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = DIAGNOSTIC_DIR / f"optica_diag_{stamp}_{safe_key}.json"
    screenshot_path = path.with_suffix(".png")
    screenshot_saved = bool(
        _safe_driver_value(
            lambda: driver.save_screenshot(str(screenshot_path)), False
        )
    )
    title = str(_safe_driver_value(lambda: driver.title, "") or "")[:240]
    current_url = str(_safe_driver_value(lambda: driver.current_url, "") or "")
    body_text = str(
        _safe_driver_value(
            lambda: driver.find_element(By.TAG_NAME, "body").text,
            "",
        )
        or ""
    ).lower()
    combined = f"{title.lower()} {body_text}"
    markers = {
        "captcha_or_bot_check": any(
            value in combined
            for value in (
                "access denied",
                "robot",
                "cloudflare",
                "distil",
                "attention required",
                "security check",
                "made us think that you are a bot",
            )
        ),
        "rate_limit": any(
            value in combined
            for value in (
                "downloading timeout",
                "heavy usage",
                "please try again in a few minutes",
            )
        ),
        "subscription_or_access_message": any(
            value in combined
            for value in (
                "purchase this article",
                "institutional access",
                "sign in to access",
                "access through your institution",
                "not available",
            )
        ),
        "pdf_open_pending": "your pdf will open shortly" in combined,
    }
    details = {}
    for key, value in (failure_details or {}).items():
        if "url" in str(key).lower():
            details[str(key)] = sanitize_url(str(value))
        elif isinstance(value, (str, int, float, bool)) or value is None:
            details[str(key)] = value

    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "trigger": trigger,
        "article_number": str(article_number),
        "failure_reason": str(failure_reason),
        "failure_details": details,
        "stored_url": sanitize_url(stored_url),
        "resolved_url": sanitize_url(resolved_url),
        "fallback_url": sanitize_url(fallback_url),
        "browser": {
            "title": title,
            "current_url": sanitize_url(current_url),
            "markers": markers,
            "pdf_link_candidates": _link_candidates(driver),
            "embedded_resource_candidates": _embedded_resource_candidates(driver),
            "screenshot": screenshot_path.name if screenshot_saved else None,
        },
        "safety": {
            "navigation_performed": False,
            "link_clicked": False,
            "pdf_requested": False,
            "network_requests_added": 0,
            "cookies_recorded": False,
            "raw_html_recorded": False,
            "screenshot_saved": screenshot_saved,
        },
    }

    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)
    return path
