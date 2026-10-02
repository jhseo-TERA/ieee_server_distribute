# -*- coding: utf-8 -*-
"""
Optica PDF 다운로더 (연세대 프록시) — 즐겨찾기(★) 전용

scripts/download_pdfs.py(IEEE)와 동일한 정책(즐겨찾기만 대상, 하드코딩)이지만
Optica의 PDF 다운로드 링크(viewmedia.cfm)는 JS 챌린지가 있어 requests만으로는
통과하지 못하고 실제 브라우저 navigate가 필요함 — 조사 결과:
  - requests + 쿠키만으로 viewmedia.cfm 요청 -> 202 + "Please wait..." JS
    챌린지 페이지만 반환됨 (checkjs.cfm 을 fetch 해야 진짜 PDF로 리다이렉트).
  - 실제 Selenium 브라우저로 같은 링크를 열면 캡차 없이 자동 통과되고
    directpdfaccess/<토큰>/<키>.pdf 형태의 진짜 PDF 직링크로 리다이렉트됨.

흐름:
  1) Selenium(Edge) 기본 설정으로 연세대 프록시 로그인
  2) DB에서 is_favorite=1, pdf_available=0, article_number가 숫자가 아닌
     (Optica uri 키) 논문을 골라 하나씩:
       a) abstract 페이지로 이동 (uri 키에서 URL 재구성)
       b) "Get PDF" 링크로 이동 (JS 챌린지는 브라우저가 알아서 통과)
       c) 리다이렉트된 최종 PDF URL 확인
       d) 그 시점 쿠키를 requests.Session 으로 이식해 최종 URL 재요청,
          %PDF 매직바이트 검증 후 optica-pdf/<키>.pdf 저장 + DB 갱신
  3) CAPTCHA 또는 속도제한이 뜨면 즉시 중단하고 지속형 쿨다운 기록.

headless는 지원하지 않음 — JS 챌린지/캡차 대응에 실제 화면이 필요함.

이전 실험 기록(현재 routine에는 적용하지 않음):
  1) UA를 고정 스푸핑하지 않음 — 예전엔 "Chrome/124.0"으로 위장했는데 실제 설치된
     Edge(150.x)와 버전이 달라 Sec-CH-UA Client Hints와 모순되는 게 오히려 봇 신호였음.
     지금은 실제 브라우저의 UA를 그대로 사용(위장 없음)해서 일관성을 맞춤.
  2) 항목당 랜덤 지연 + N건마다 랜덤 배치 휴식(지터) — 너무 규칙적인 간격 자체가
     봇 패턴이라 고정값 대신 범위 랜덤 사용.
  3) abstract 페이지 로드 후 PDF 링크를 찾기 전에 사람처럼 스크롤/마우스 이동을
     흉내내고, 링크도 직접 URL 이동 대신 실제 마우스 클릭(ActionChains)으로 처리.
  4) 영속 브라우저 프로필(--user-data-dir)을 사용해 매 실행마다 "처음 보는 기기"로
     보이지 않도록 세션/쿠키 연속성을 유지.

사용 예:
  .venv\\Scripts\\python.exe scripts\\run_pdf_download_routine.py --provider optica --limit 5
"""
import os
import re
import sys
import time
import random
import argparse
import json
from urllib.parse import parse_qs, quote, urlparse, urljoin

import pymysql
import requests
from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait

try:
    from .pdf_proxy_auth import bootstrap_failure
    from .download_guard import (
        DownloadGuard,
        add_guard_arguments,
        bounded_download_limit,
        clear_item_failure,
        deferred_item_ids,
        download_result_code,
        invoked_by_daily_routine,
        load_repair_ids,
        mark_repair_complete,
        record_item_failure,
        validate_pdf_file,
    )
    from .download_pdfs import (
        build_session as build_ieee_session,
        download_one as download_ieee_pdf,
        selenium_login as ieee_selenium_login,
    )
    from .optica_diagnostics import capture_optica_diagnostic
except ImportError:  # Direct execution from the scripts directory.
    from pdf_proxy_auth import bootstrap_failure
    from download_guard import (
        DownloadGuard,
        add_guard_arguments,
        bounded_download_limit,
        clear_item_failure,
        deferred_item_ids,
        download_result_code,
        invoked_by_daily_routine,
        load_repair_ids,
        mark_repair_complete,
        record_item_failure,
        validate_pdf_file,
    )
    from download_pdfs import (
        build_session as build_ieee_session,
        download_one as download_ieee_pdf,
        selenium_login as ieee_selenium_login,
    )
    from optica_diagnostics import capture_optica_diagnostic

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PROVIDER = "optica"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PDF_DIR = os.path.join(ROOT, "optica-pdf")
PROFILE_DIR = os.path.join(ROOT, ".edge_profile_optica")  # 영속 프로필(세션 연속성)
os.makedirs(PDF_DIR, exist_ok=True)
os.makedirs(PROFILE_DIR, exist_ok=True)
load_dotenv(os.path.join(ROOT, ".env"))

PROXY_PREFIX = "https://access.yonsei.ac.kr/link.n2s?url="
OPTICA_PROXY_HOST = "opg-optica-org-ssl.access.yonsei.ac.kr"
OPTICA_PROXY_ENTRY_URL = f"{PROXY_PREFIX}https://opg.optica.org/prj/browse.cfm"
IEEE_DOCUMENT_RE = re.compile(
    r"https?://(?:www\.)?ieeexplore\.ieee\.org/document/(\d+)", re.IGNORECASE
)
ALLOWED_SOURCE_HOSTS = {"opg.optica.org", "www.opg.optica.org", "ieeexplore.ieee.org", "www.ieeexplore.ieee.org"}

# Conservative black-box pacing after Optica returned an IP heavy-usage
# timeout near the previous 9-12 logical attempts/hour rate. Eleven to
# twelve minutes between papers keeps the target below six papers/hour.
OPTICA_MIN_DELAY_SECONDS = 660.0
OPTICA_MAX_DELAY_SECONDS = 720.0
OPTICA_BATCH_PAUSE_MIN_SECONDS = 660.0
OPTICA_BATCH_PAUSE_MAX_SECONDS = 720.0
OPTICA_PDF_REDIRECT_TIMEOUT_SECONDS = 60.0
OPTICA_PROXY_REFRESH_SECONDS = 2 * 3600.0
OPTICA_MAX_REAUTH_PER_RUN = 1
OPTICA_HEAVY_USAGE_COOLDOWN_SECONDS = 48 * 3600
OPTICA_CAPTCHA_COOLDOWN_SECONDS = 72 * 3600
DIAGNOSTIC_FAILURE_REASONS = frozenset({
    "pdf_link_without_href",
    "proxy_reauth_failed",
    "proxy_session_expired",
    "redirect_stalled",
    "viewer_pdf_endpoint_not_found",
    "response_not_pdf",
})

DB = dict(
    host=os.getenv("DB_HOST", "127.0.0.1"),
    port=int(os.getenv("DB_PORT", "3306")),
    user=os.getenv("DB_USER", "root"),
    password=os.getenv("DB_PASSWORD", ""),
    database=os.getenv("DB_NAME", "ieee_repo"),
    charset="utf8mb4",
)


def db_connect():
    return pymysql.connect(**DB)


def fetch_targets(conn, args):
    """다운로드 대상(즐겨찾기 + Optica + pdf 미보유) 조회.
    예전엔 article_number가 숫자가 아니면 다 Optica로 간주했는데, Nature도
    비숫자 키를 쓰기 시작해서 source_system 컬럼으로 명시적으로 구분함."""
    repair_ids = load_repair_ids(PROVIDER)
    deferred_ids = deferred_item_ids(PROVIDER)
    where = ["pdf_available = 0", "source_system = 'optica'"]
    params = []
    ieee_route = "(source_name = 'JLT' AND (COALESCE(url, '') LIKE 'https://ieeexplore.ieee.org/document/%%' OR COALESCE(url, '') LIKE 'https://www.ieeexplore.ieee.org/document/%%'))"
    where.append(ieee_route if getattr(args, "route", "optica") == "jlt-ieee" else "NOT " + ieee_route)
    if repair_ids:
        placeholders = ",".join(["%s"] * len(repair_ids))
        where.append(f"(is_favorite = 1 OR article_number IN ({placeholders}))")
        params.extend(repair_ids)
    else:
        where.append("is_favorite = 1")
    if args.source:
        where.append("source_name = %s")
        params.append(args.source)
    if args.min_year:
        where.append("(year+0) >= %s")
        params.append(int(args.min_year))
    if deferred_ids:
        placeholders = ",".join(["%s"] * len(deferred_ids))
        where.append(f"article_number NOT IN ({placeholders})")
        params.extend(deferred_ids)
    order_sql = ""
    if repair_ids:
        placeholders = ",".join(["%s"] * len(repair_ids))
        order_sql = f"CASE WHEN article_number IN ({placeholders}) THEN 0 ELSE 1 END, "
        params.extend(repair_ids)
    sql = (
        "SELECT article_number, url, source_name, year, LEFT(title,60) "
        "FROM papers WHERE " + " AND ".join(where) +
        " ORDER BY " + order_sql + "(year+0) DESC, id DESC"
    )
    sql += " LIMIT %s"
    params.append(int(args.limit))
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def optica_path_segment(article_number: str) -> str:
    """Return the publisher path segment; Optica and OFC live at the root."""
    segment = str(article_number or "").split("-", 1)[0].strip().lower()
    return "" if segment in {"optica", "ofc"} else segment


def abstract_url(article_number: str) -> str:
    """uri 키에서 abstract 페이지 URL 재구성.
    'Optica' 저널 자체는 저널 세그먼트 없이 /abstract.cfm 로 접근함(확인됨)."""
    path_segment = optica_path_segment(article_number)
    path_prefix = f"/{path_segment}" if path_segment else ""
    return f"https://opg.optica.org{path_prefix}/abstract.cfm?uri={quote(str(article_number))}"


def source_url(article_number: str, stored_url: str | None) -> str:
    """Return a trusted publisher URL, falling back only for legacy Optica keys.

    JLT uses a stable DOI-derived database key that is not an Optica ``uri``.
    Its stored URL is therefore authoritative: finalized records point at an
    OPG ``jlt-<volume>-<issue>-<page>`` URL, while early-access records point at
    IEEE Xplore.
    """
    candidate = str(stored_url or "").strip()
    try:
        parsed = urlparse(candidate)
    except ValueError:
        parsed = None
    if (
        parsed
        and parsed.scheme == "https"
        and (parsed.hostname or "").lower() in ALLOWED_SOURCE_HOSTS
    ):
        return candidate
    return abstract_url(article_number)


def ieee_document_id(url: str) -> str | None:
    match = IEEE_DOCUMENT_RE.match(str(url or "").strip())
    return match.group(1) if match else None


def optica_uri(url: str) -> str | None:
    try:
        query = parse_qs(urlparse(url).query)
    except ValueError:
        return None
    values = []
    for key, candidates in query.items():
        if key.lower() == "uri":
            values = candidates
            break
    value = values[0].strip() if values else ""
    return value or None


def fallback_viewmedia_url(host: str, article_number: str, stored_url: str | None = None) -> str:
    if stored_url:
        parsed = urlparse(stored_url)
        query = {key.lower(): values for key, values in parse_qs(parsed.query).items()}
        doi = (query.get('doi') or [''])[0]
        if ((parsed.hostname or '').lower() in ALLOWED_SOURCE_HOSTS
                and parsed.path.endswith('/abstract.cfm')
                and re.fullmatch(r'10\.1364/[a-zA-Z0-9._-]+', doi)):
            path = parsed.path.removesuffix('abstract.cfm') + 'viewmedia.cfm'
            return f'https://{host}{path}?doi={quote(doi)}&seq=0'
    path_segment = optica_path_segment(article_number)
    path_prefix = f"/{path_segment}" if path_segment else ""
    return f"https://{host}{path_prefix}/viewmedia.cfm?uri={quote(str(article_number))}&seq=0"


def is_optica_article_url(value: str) -> bool:
    """Return whether a browser URL is still on an Optica article host.

    The Yonsei access gateway may send an expired interactive session back to
    ``library.yonsei.ac.kr``.  That host must never be used to construct an
    Optica ``viewmedia.cfm`` fallback URL.
    """
    try:
        host = (urlparse(str(value or "")).hostname or "").lower()
    except ValueError:
        return False
    return host == OPTICA_PROXY_HOST or host in {
        "opg.optica.org",
        "www.opg.optica.org",
    }


def proxy_refresh_due(session_started: float, probe_done: bool, *, now=None) -> bool:
    """Request one proactive session health check after two elapsed hours."""
    if probe_done:
        return False
    current = time.monotonic() if now is None else float(now)
    return current - float(session_started) >= OPTICA_PROXY_REFRESH_SECONDS


def check_captcha(driver):
    """py_00_src/proxy_access_test_v28_Optica.py 와 동일한 차단 탐지 로직
    (title/page_source 읽기 자체가 실패해도 크래시하지 않도록 방어)."""
    try:
        current_title = driver.title.lower()
    except Exception:
        return
    blocked_titles = ["access denied", "robot", "cloudflare", "distil", "attention required", "security check"]
    is_blocked = any(bt in current_title for bt in blocked_titles)

    if not is_blocked:
        block_keywords = [
            "we apologize for the inconvenience",
            "activity and behavior on this site",
            "made us think that you are a bot",
            "malicious behavior",
        ]
        try:
            page_content = driver.page_source.lower()
        except Exception:
            return
        if any(kw in page_content for kw in block_keywords):
            is_blocked = True

    if is_blocked:
        print(f"\n[차단 감지] Optica 보안 페이지: {driver.title}")
        print("[안전 중단] routine에서는 CAPTCHA를 풀거나 반복 요청하지 않습니다.")
        return True
    return False


RATE_LIMIT_KEYWORDS = [
    "downloading timeout",
    "heavy usage",
    "subject to a downloading timeout",
    "please try again in a few minutes",
]


def check_rate_limit(driver):
    """Optica의 '이 IP는 heavy usage로 다운로드 타임아웃 중' 안내 감지.
    캡차와 달리 사람이 풀 수 있는 게 아니라 순수 시간 제한이라, input() 없이
    길게 sleep 후 자동 재확인한다(무인 백그라운드 실행 대응).
    반환값: True면 대기 후에도 여전히 제한 상태(호출부에서 이 항목 포기 권장)."""
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
        limited = any(kw in body_text for kw in RATE_LIMIT_KEYWORDS)
    except Exception:
        return False
    if limited:
        print("\n[속도제한 감지] Optica heavy-usage timeout - 즉시 실행을 중단합니다.")
    return limited


def proxy_entry_state(driver):
    """Return ``ready`` or ``login`` once the access gateway settles."""
    try:
        if is_optica_article_url(driver.current_url):
            return "ready"
    except Exception:
        pass
    try:
        if driver.find_elements(By.ID, "id"):
            return "login"
    except Exception:
        pass
    return False


def refresh_proxy_session(driver, *, timeout=20):
    """Open the Optica proxy entry and authenticate only when prompted.

    Returns ``(ready, login_performed)``.  A two-hour probe may reach the
    expected host without displaying a form; in that case it is only a health
    check and callers must not assume that the server-side expiry was renewed.
    """
    print("[*] 연세대 프록시 세션 확인 중...")
    driver.get(OPTICA_PROXY_ENTRY_URL)
    try:
        state = WebDriverWait(driver, timeout).until(proxy_entry_state)
    except Exception:
        print(f"    [실패] 프록시 진입 상태를 확인하지 못했습니다: {driver.current_url[:120]}")
        return False, False

    login_performed = False
    if state == "login":
        yid, ypw = os.getenv("YONSEI_ID"), os.getenv("YONSEI_PW")
        if not yid or not ypw:
            print("    [실패] .env 에 YONSEI_ID / YONSEI_PW 가 없습니다.")
            return False, False
        try:
            driver.find_element(By.ID, "id").send_keys(yid)
            driver.find_element(By.ID, "password").send_keys(ypw)
            driver.find_element(
                By.XPATH, "//input[@type='submit' and @value='로그인']"
            ).click()
            WebDriverWait(driver, timeout).until(
                lambda current: is_optica_article_url(current.current_url)
            )
            login_performed = True
        except Exception:
            print(f"    [실패] 프록시 재로그인에 실패했습니다: {driver.current_url[:120]}")
            return False, False

    if not is_optica_article_url(driver.current_url):
        print(f"    [실패] Optica 프록시가 아닌 위치입니다: {driver.current_url[:120]}")
        return False, login_performed

    close_other_windows(driver)
    drain_performance_log(driver)
    action = "로그인 완료" if login_performed else "기존 세션 정상"
    print(f"    {action}: {driver.current_url[:120]}")
    return True, login_performed


def selenium_login(use_profile=True):
    if PROVIDER == "jlt_ieee":
        return ieee_selenium_login()
    options = Options()
    options.add_argument("--window-size=1200,900")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    # 설치된 브라우저의 기본 UA를 그대로 사용한다.
    if use_profile:
        options.add_argument(f"--user-data-dir={PROFILE_DIR}")
        options.add_argument("--profile-directory=Default")

    print("[*] Edge 브라우저 기동...")
    driver = webdriver.Edge(options=options)
    ready, _ = refresh_proxy_session(driver)
    if not ready:
        try:
            driver.quit()
        except Exception:
            pass
        raise RuntimeError("Optica proxy login failed")
    return driver


def transplant_cookies(driver):
    real_ua = None
    try:
        real_ua = driver.execute_script("return navigator.userAgent")
    except Exception:
        pass
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": real_ua or "Mozilla/5.0",
        "Accept-Language": "ko,en;q=0.9",
    })
    for c in driver.get_cookies():
        try:
            sess.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
        except Exception:
            continue
    return sess


def looks_like_pdf(resp):
    ct = (resp.headers.get("Content-Type") or "").lower()
    if "application/pdf" in ct:
        return True
    return resp.content[:4] == b"%PDF"


def trusted_optica_download_url(value):
    """Allow only HTTPS Optica or Yonsei-proxied resources."""
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        host in ALLOWED_SOURCE_HOSTS or host.endswith(".access.yonsei.ac.kr")
    )


def is_direct_pdf_url(value):
    candidate = str(value or "").strip()
    lowered = candidate.lower()
    return trusted_optica_download_url(candidate) and (
        "directpdfaccess" in lowered or urlparse(lowered).path.endswith(".pdf")
    )


def parse_pdf_response_urls(entries):
    """Extract strong PDF candidates from Edge performance-log events."""
    candidates = []
    seen = set()
    for entry in entries or ():
        try:
            envelope = json.loads(entry.get("message", "{}"))
            message = envelope.get("message", {})
            if message.get("method") != "Network.responseReceived":
                continue
            response = message.get("params", {}).get("response", {})
            url = str(response.get("url") or "").strip()
            mime_type = str(response.get("mimeType") or "").lower()
        except (AttributeError, TypeError, ValueError):
            continue
        lowered_url = url.lower()
        strong_pdf_signal = (
            "application/pdf" in mime_type
            or "directpdfaccess" in lowered_url
            or urlparse(lowered_url).path.endswith(".pdf")
        )
        if not strong_pdf_signal or not trusted_optica_download_url(url):
            continue
        if url not in seen:
            seen.add(url)
            candidates.append(url)
    return candidates[-3:]


def drain_performance_log(driver):
    """Discard prior-page events so candidates belong to the current item."""
    try:
        driver.get_log("performance")
    except Exception:
        pass


def browser_pdf_candidates(driver):
    """Find PDF resources already observed by Edge without extra navigation."""
    try:
        entries = driver.get_log("performance")
    except Exception:
        entries = []
    candidates = parse_pdf_response_urls(entries)
    seen = set(candidates)
    for selector, attribute in (
        ("iframe[src]", "src"),
        ("embed[src]", "src"),
        ("object[data]", "data"),
    ):
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            continue
        for element in elements:
            try:
                value = element.get_attribute(attribute)
            except Exception:
                continue
            candidate = urljoin(driver.current_url, str(value or "").strip())
            lowered = candidate.lower()
            if not (
                "directpdfaccess" in lowered
                or urlparse(lowered).path.endswith(".pdf")
            ):
                continue
            if trusted_optica_download_url(candidate) and candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)
            if len(candidates) >= 3:
                return candidates
    return candidates


def background_pdf_tab_candidates(driver):
    """Inspect Edge targets without visibly switching the selected tab."""
    try:
        target_infos = driver.execute_cdp_cmd("Target.getTargets", {}).get(
            "targetInfos", []
        )
    except Exception:
        return []
    candidates = []
    for target in target_infos:
        if not isinstance(target, dict) or target.get("type") != "page":
            continue
        url = str(target.get("url") or "").strip()
        if is_direct_pdf_url(url) and url not in candidates:
            candidates.append(url)
    return candidates[-3:]


def wait_for_pdf_candidates(
    driver,
    timeout=OPTICA_PDF_REDIRECT_TIMEOUT_SECONDS,
    poll_interval=0.5,
):
    """Wait for Optica's delayed redirect without visible tab switching."""
    deadline = time.monotonic() + max(0.0, timeout)
    candidates = []
    seen = set()
    last_url = ""
    while True:
        try:
            current_url = str(driver.current_url or "")
        except Exception:
            current_url = ""
        last_url = current_url or last_url
        if is_direct_pdf_url(current_url):
            return current_url, [current_url]

        observed = (
            browser_pdf_candidates(driver) + background_pdf_tab_candidates(driver)
        )
        for candidate in observed:
            if candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)
        if candidates:
            return candidates[-1], candidates[:3]
        if time.monotonic() >= deadline:
            return last_url, []
        time.sleep(max(0.05, poll_interval))


def close_other_windows(driver):
    """Close stale tabs only after the PDF viewer has finished initializing."""
    try:
        current_handle = driver.current_window_handle
        handles = list(driver.window_handles)
    except Exception:
        return
    for handle in handles:
        if handle == current_handle:
            continue
        try:
            result = driver.execute_cdp_cmd(
                "Target.closeTarget", {"targetId": handle}
            )
            if result.get("success"):
                continue
        except Exception:
            pass
        try:
            driver.switch_to.window(handle)
            driver.close()
            driver.switch_to.window(current_handle)
        except Exception as exc:
            print(f"\n    [경고] 이전 탭 정리 실패({handle}): {exc}")


def download_one(driver, key, stored_url=None):
    """PDF bytes, FAILED details, or BLOCKED. 브라우저로 정상 다운로드 링크를 연 뒤,
    리다이렉트된 최종 URL을 requests 로 다시 받는다.

    JLT early-access rows are hosted by IEEE Xplore even though their metadata
    remains in the Optica/JLT collection. Those rows use the IEEE PDF backend
    but are still saved under the stable JLT database key by the caller.
    """
    abs_url = source_url(key, stored_url)
    document_id = ieee_document_id(abs_url)
    if document_id:
        driver.get(f"{PROXY_PREFIX}{abs_url}")
        time.sleep(random.uniform(1.5, 2.5))
        session, host = build_ieee_session(driver, abs_url)
        result = download_ieee_pdf(session, host, document_id)
        if result == "RELOGIN":
            driver.get(f"{PROXY_PREFIX}{abs_url}")
            time.sleep(5)
            session, host = build_ieee_session(driver, abs_url)
            result = download_ieee_pdf(session, host, document_id)
        if result == "RELOGIN":
            return (
                "FAILED",
                "ieee_relogin_failed",
                {"document_id": document_id, "resolved_url": abs_url},
            )
        if result is None:
            return (
                "FAILED",
                "ieee_access_or_format",
                {"document_id": document_id, "resolved_url": abs_url},
            )
        return result

    drain_performance_log(driver)
    driver.get(f"{PROXY_PREFIX}{abs_url}")
    time.sleep(random.uniform(1.5, 2.5))
    if check_captcha(driver):
        return ("BLOCKED", OPTICA_CAPTCHA_COOLDOWN_SECONDS, "captcha_or_bot_check")
    if check_rate_limit(driver):
        return (
            "BLOCKED",
            OPTICA_HEAVY_USAGE_COOLDOWN_SECONDS,
            "heavy_usage_timeout",
        )

    try:
        landing_url = str(driver.current_url or "")
    except Exception:
        landing_url = ""
    if not is_optica_article_url(landing_url):
        return (
            "SESSION_EXPIRED",
            "proxy_session_expired",
            {"resolved_url": abs_url, "final_url": landing_url},
        )

    uri_key = optica_uri(abs_url) or key
    pdf_link_url = None
    link_el = None
    try:
        links = driver.find_elements(By.LINK_TEXT, "Get PDF")
        if links:
            link_el = links[0]
            pdf_link_url = link_el.get_attribute("href")
    except Exception:
        pass

    if link_el is not None:
        handles_before = driver.window_handles
        clicked = False
        try:
            # 실제 URL 이동 대신 마우스로 링크에 접근해 클릭(호버 -> 클릭 이벤트 재현)
            ActionChains(driver).move_to_element(link_el).pause(
                random.uniform(0.3, 0.8)).click(link_el).perform()
            clicked = True
        except Exception:
            pass
        if clicked:
            # 링크가 새 탭(target=_blank)으로 열렸으면 그 탭으로 전환해야
            # driver.current_url 이 실제 PDF 페이지를 가리킨다. 원래 탭은
            # view_article 초기화가 끝날 때까지 window.opener 로 필요할 수 있으므로
            # 여기서 닫지 않고 PDF 네트워크 후보를 관측한 뒤 정리한다.
            time.sleep(0.5)
            handles_after = driver.window_handles
            if len(handles_after) > len(handles_before):
                new_handle = [h for h in handles_after if h not in handles_before][0]
                driver.switch_to.window(new_handle)
        elif pdf_link_url:
            driver.get(pdf_link_url)  # 클릭 실패 시 직접 이동으로 대체
        else:
            return (
                "FAILED",
                "pdf_link_without_href",
                {"resolved_url": abs_url, "pdf_link_found": True},
            )
    else:
        host = urlparse(driver.current_url).netloc or "opg-optica-org-ssl.access.yonsei.ac.kr"
        pdf_link_url = fallback_viewmedia_url(host, uri_key, abs_url)
        driver.get(pdf_link_url)

    final_url, candidate_urls = wait_for_pdf_candidates(driver)
    if check_captcha(driver):
        return ("BLOCKED", OPTICA_CAPTCHA_COOLDOWN_SECONDS, "captcha_or_bot_check")
    if check_rate_limit(driver):
        return (
            "BLOCKED",
            OPTICA_HEAVY_USAGE_COOLDOWN_SECONDS,
            "heavy_usage_timeout",
        )

    if not candidate_urls and not is_optica_article_url(final_url):
        return (
            "SESSION_EXPIRED",
            "proxy_session_expired",
            {
                "resolved_url": abs_url,
                "pdf_link_url": pdf_link_url,
                "final_url": final_url,
            },
        )

    if (
        ("viewmedia.cfm" in final_url or "abstract.cfm" in final_url)
        and not candidate_urls
    ):
        # 리다이렉트가 안 됐으면 실패(접근권한 없음 등)
        close_other_windows(driver)
        return (
            "FAILED",
            "redirect_stalled",
            {
                "resolved_url": abs_url,
                "pdf_link_url": pdf_link_url,
                "final_url": final_url,
                "pdf_link_found": link_el is not None,
            },
        )
    if (
        "view_article.cfm" not in final_url.lower()
        and trusted_optica_download_url(final_url)
        and final_url not in candidate_urls
    ):
        candidate_urls.append(final_url)
    candidate_urls = candidate_urls[:3]
    if not candidate_urls:
        return (
            "FAILED",
            "viewer_pdf_endpoint_not_found",
            {
                "resolved_url": abs_url,
                "final_url": final_url,
                "network_pdf_candidate_count": 0,
            },
        )

    sess = transplant_cookies(driver)
    last_response = None
    try:
        for candidate_url in candidate_urls:
            r = sess.get(
                candidate_url,
                headers={"Referer": pdf_link_url},
                timeout=90,
                allow_redirects=True,
            )
            if looks_like_pdf(r):
                return r.content
            last_response = r
        return (
            "FAILED",
            "response_not_pdf",
            {
                "status_code": last_response.status_code,
                "content_type": last_response.headers.get("Content-Type"),
                "final_url": last_response.url,
                "network_pdf_candidate_count": len(candidate_urls),
            },
        )
    except Exception as exc:
        return (
            "FAILED",
            "request_exception",
            {
                "exception": type(exc).__name__,
                "final_url": final_url,
                "network_pdf_candidate_count": len(candidate_urls),
            },
        )
    finally:
        close_other_windows(driver)


def capture_current_page_diagnostic(
    driver,
    *,
    article_number,
    stored_url,
    failure_reason,
    failure_details=None,
    trigger="three_consecutive_failures",
):
    """Capture already-open page state without navigation or another request."""
    resolved_url = source_url(str(article_number), stored_url)
    uri_key = optica_uri(resolved_url) or str(article_number)
    try:
        current_host = urlparse(driver.current_url).netloc
    except Exception:
        current_host = ""
    diagnostic_host = (
        current_host
        if is_optica_article_url(f"https://{current_host}/")
        else OPTICA_PROXY_HOST
    )
    fallback_url = fallback_viewmedia_url(diagnostic_host, uri_key, resolved_url)
    try:
        diagnostic_path = capture_optica_diagnostic(
            driver,
            article_number=str(article_number),
            stored_url=stored_url,
            resolved_url=resolved_url,
            fallback_url=fallback_url,
            failure_reason=str(failure_reason),
            failure_details=failure_details or {},
            trigger=trigger,
        )
        print(
            f"[DIAGNOSE-ONLY] 추가 요청 없이 현재 페이지 진단 저장: "
            f"{diagnostic_path}"
        )
        return diagnostic_path
    except Exception as exc:
        print(f"[DIAGNOSE-ONLY 경고] 진단 저장 실패: {type(exc).__name__}")
        return None


def is_proxy_session_expired_result(result) -> bool:
    return (
        isinstance(result, tuple)
        and len(result) > 1
        and result[0] == "SESSION_EXPIRED"
        and result[1] == "proxy_session_expired"
    )


def download_with_proxy_recovery(
    driver,
    article_number,
    stored_url,
    *,
    allow_reauth,
):
    """Download one logical paper and recover one expired proxy session.

    An access-gateway detour never reached the publisher article, so the retry
    remains the same logical attempt.  The caller owns the run-wide reauth
    budget and stops the provider when recovery cannot establish a valid host.
    """
    result = download_one(driver, article_number, stored_url)
    if not is_proxy_session_expired_result(result):
        return result, False, False

    failure_details = (
        result[2]
        if len(result) > 2 and isinstance(result[2], dict)
        else {}
    )
    capture_current_page_diagnostic(
        driver,
        article_number=article_number,
        stored_url=stored_url,
        failure_reason="proxy_session_expired",
        failure_details=failure_details,
        trigger="reactive_session_recovery",
    )
    if not allow_reauth:
        return (
            (
                "FAILED",
                "proxy_reauth_failed",
                {**failure_details, "reason": "run_reauth_limit_reached"},
            ),
            False,
            True,
        )

    print("세션 만료 감지 - 프록시 재로그인 후 같은 논문을 다시 시도합니다.")
    ready, _ = refresh_proxy_session(driver)
    if not ready:
        return (
            (
                "FAILED",
                "proxy_reauth_failed",
                {**failure_details, "reason": "proxy_entry_not_restored"},
            ),
            True,
            True,
        )

    retry_result = download_one(driver, article_number, stored_url)
    if is_proxy_session_expired_result(retry_result):
        retry_details = (
            retry_result[2]
            if len(retry_result) > 2 and isinstance(retry_result[2], dict)
            else {}
        )
        capture_current_page_diagnostic(
            driver,
            article_number=article_number,
            stored_url=stored_url,
            failure_reason="proxy_reauth_failed",
            failure_details=retry_details,
            trigger="reactive_session_recovery_failed",
        )
        return (
            (
                "FAILED",
                "proxy_reauth_failed",
                {**retry_details, "reason": "retry_left_optica_host"},
            ),
            True,
            True,
        )
    return retry_result, True, False


def record_failure_and_maybe_diagnose(
    guard,
    driver,
    *,
    article_number,
    stored_url,
    failure_reason,
    failure_details=None,
):
    """Apply item backoff and preserve useful local state before it disappears.

    Navigation/viewer failures are captured on their first occurrence because
    the next item replaces the current DOM. Other failures retain the quieter
    circuit-breaker-only diagnostic behavior. Capture is local-only and does
    not navigate or add a publisher request.
    """
    retry_after = record_item_failure(
        PROVIDER, str(article_number), str(failure_reason)
    )
    should_stop = guard.failure()
    diagnostic_path = None
    if should_stop or str(failure_reason) in DIAGNOSTIC_FAILURE_REASONS:
        diagnostic_path = capture_current_page_diagnostic(
            driver,
            article_number=article_number,
            stored_url=stored_url,
            failure_reason=failure_reason,
            failure_details=failure_details,
            trigger=(
                "three_consecutive_failures"
                if should_stop
                else "first_structural_failure"
            ),
        )
    # A failed viewer path may return before download_one's requests cleanup.
    # Preserve diagnostics first, then discard stale tabs before the next item.
    close_other_windows(driver)
    return retry_after, should_stop, diagnostic_path


def pace_between_items(guard, args, *, done, index, total):
    """Apply publisher pacing only when another item will actually follow."""
    if index >= total:
        return 0.0
    if args.batch_size > 0 and done % args.batch_size == 0:
        pause = random.uniform(args.batch_pause_min, args.batch_pause_max)
        print(f"    [배치 휴식] {done}건 처리 -> {pause:.0f}초 대기...")
        time.sleep(pause)
        return pause
    guard.pace()
    return None


def main() -> int:
    global PROVIDER
    ap = argparse.ArgumentParser(description="Optica PDF 다운로더 (연세대 프록시) — 즐겨찾기(★) 전용")
    ap.add_argument(
        "--limit", type=bounded_download_limit, default=30,
        help="최대 다운로드 수 (1~30, 기본 30)",
    )
    ap.add_argument("--route", choices=("optica", "jlt-ieee"), default="optica")
    ap.add_argument("--source", help="특정 출처만 (예: OE, OL, PR, JLT, Optica)")
    ap.add_argument("--min-year", dest="min_year", help="이 연도 이상")
    add_guard_arguments(
        ap,
        min_delay=OPTICA_MIN_DELAY_SECONDS,
        max_delay=OPTICA_MAX_DELAY_SECONDS,
    )
    ap.add_argument("--batch-size", dest="batch_size", type=int, default=10,
                     help="이 건수마다 배치 휴식 삽입 (기본 10, 0이면 비활성)")
    ap.add_argument("--batch-pause-min", dest="batch_pause_min", type=float,
                     default=OPTICA_BATCH_PAUSE_MIN_SECONDS,
                     help="배치 휴식 최소 초 (기본 660)")
    ap.add_argument("--batch-pause-max", dest="batch_pause_max", type=float,
                     default=OPTICA_BATCH_PAUSE_MAX_SECONDS,
                     help="배치 휴식 최대 초 (기본 720)")
    ap.add_argument("--no-profile", dest="no_profile", action="store_true",
                     help="영속 브라우저 프로필(.edge_profile_optica) 사용 안 함")
    args = ap.parse_args()
    PROVIDER = "jlt_ieee" if args.route == "jlt-ieee" else "optica"
    if PROVIDER == "jlt_ieee":
        args.min_sleep, args.max_sleep = 30, 60
    if not invoked_by_daily_routine():
        print("[실패] 개별 다운로더 직접 실행은 차단되어 있습니다. 일간 PDF routine을 사용하세요.")
        return 2

    conn = db_connect()
    deferred_count = len(deferred_item_ids(PROVIDER))
    if deferred_count:
        print(f"[*] 최근 항목별 실패 {deferred_count}건은 재시도 유예 후 큐 뒤로 넘깁니다.")
    targets = fetch_targets(conn, args)
    if not targets:
        print("[*] 다운로드 대상이 없습니다. (즐겨찾기 중 조건에 맞는 미보유 PDF 없음)")
        print("[완료] 성공 0 / 건너뜀 0 / 실패 0")
        conn.close()
        return 0
    print(f"[*] 대상 {len(targets):,}건 (상위 {args.limit}건)")
    print(f"[*] 페이싱: 항목당 {args.min_sleep:.0f}~{args.max_sleep:.0f}초 랜덤"
          + (f", {args.batch_size}건마다 {args.batch_pause_min:.0f}~{args.batch_pause_max:.0f}초 휴식"
             if args.batch_size > 0 else ""))

    guard = DownloadGuard(
        PROVIDER,
        min_delay=args.min_sleep,
        max_delay=args.max_sleep,
        cooldown_hours=args.cooldown_hours,
        max_consecutive_failures=args.max_consecutive_failures,
    )
    allowed, reason = guard.acquire()
    if not allowed:
        print(f"[*] Optica 실행 생략: {reason}")
        print("[완료] 성공 0 / 건너뜀 0 / 실패 0")
        conn.close()
        return 0

    interrupted = False
    try:
        driver = selenium_login(use_profile=not args.no_profile)
    except Exception as exc:
        guard.close()
        conn.close()
        return bootstrap_failure(PROVIDER, exc)

    proxy_session_started = time.monotonic()
    proxy_probe_done = False
    proxy_reauth_count = 0
    ok = skip = fail = 0
    upd = conn.cursor()
    try:
        for i, (arnum, stored_url, src, year, title) in enumerate(targets, 1):
            out = os.path.join(PDF_DIR, f"{arnum}.pdf")

            if os.path.isfile(out) and os.path.getsize(out) > 2048:
                valid_pdf, _ = validate_pdf_file(out)
                if valid_pdf:
                    rel = os.path.relpath(out, ROOT).replace("\\", "/")
                    upd.execute(
                        "UPDATE papers SET pdf_available=1, pdf_local_path=%s "
                        "WHERE article_number=%s", (rel, str(arnum)))
                    conn.commit()
                    mark_repair_complete(PROVIDER, str(arnum))
                    clear_item_failure(PROVIDER, str(arnum))
                    skip += 1
                    print(f"  [{i}/{len(targets)}] {src} {year} #{arnum} … 기존 파일 DB 반영")
                    continue

            print(f"  [{i}/{len(targets)}] {src} {year} #{arnum} … ", end="", flush=True)
            stop_after_item = False
            data = None
            if PROVIDER == "optica" and proxy_refresh_due(proxy_session_started, proxy_probe_done):
                print("\n    [세션] 로그인 후 2시간 경과 - 선제 상태 확인")
                ready, login_performed = refresh_proxy_session(driver)
                proxy_probe_done = True
                if login_performed:
                    proxy_reauth_count += 1
                    proxy_session_started = time.monotonic()
                    proxy_probe_done = False
                if not ready:
                    data = (
                        "FAILED",
                        "proxy_reauth_failed",
                        {"reason": "two_hour_health_check_failed"},
                    )
                    stop_after_item = True
            try:
                if data is None:
                    data, reauth_used, recovery_failed = download_with_proxy_recovery(
                        driver,
                        arnum,
                        stored_url,
                        allow_reauth=(
                            proxy_reauth_count < OPTICA_MAX_REAUTH_PER_RUN
                        ),
                    )
                    if reauth_used:
                        proxy_reauth_count += 1
                        proxy_session_started = time.monotonic()
                        proxy_probe_done = False
                    stop_after_item = stop_after_item or recovery_failed
            except Exception as e:
                print(f"브라우저 오류({type(e).__name__}) - 세션 재기동 시도")
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = selenium_login(use_profile=not args.no_profile)
                proxy_session_started = time.monotonic()
                proxy_probe_done = False
                data = (
                    "FAILED",
                    "browser_exception",
                    {"exception": type(e).__name__},
                )

            if isinstance(data, tuple) and data[0] == "BLOCKED":
                print("차단/속도제한 감지")
                fail += 1
                block_reason = str(data[2]) if len(data) > 2 else "provider_blocked"
                capture_current_page_diagnostic(
                    driver,
                    article_number=arnum,
                    stored_url=stored_url,
                    failure_reason=block_reason,
                    failure_details={"cooldown_seconds": data[1]},
                    trigger="immediate_provider_block",
                )
                cooldown_reason = (
                    "Optica CAPTCHA 감지"
                    if block_reason == "captcha_or_bot_check"
                    else "Optica heavy-usage 속도제한 감지"
                )
                guard.cooldown(cooldown_reason, seconds=data[1])
                break

            if isinstance(data, bytes) and data.startswith(b"%PDF"):
                temporary_out = out + ".part"
                with open(temporary_out, "wb") as f:
                    f.write(data)
                valid_pdf, validation_error = validate_pdf_file(temporary_out)
                if not valid_pdf:
                    try:
                        os.remove(temporary_out)
                    except OSError:
                        pass
                    fail += 1
                    retry_after, should_stop, _ = record_failure_and_maybe_diagnose(
                        guard,
                        driver,
                        article_number=arnum,
                        stored_url=stored_url,
                        failure_reason="invalid_pdf",
                        failure_details={"validation_error": validation_error},
                    )
                    print(
                        f"실패(PDF 구조 오류: {validation_error}; "
                        f"재시도 {retry_after.astimezone().strftime('%Y-%m-%d %H:%M')})"
                    )
                    if should_stop:
                        break
                    continue
                os.replace(temporary_out, out)
                rel = os.path.relpath(out, ROOT).replace("\\", "/")
                upd.execute(
                    "UPDATE papers SET pdf_available=1, pdf_local_path=%s "
                    "WHERE article_number=%s", (rel, str(arnum)))
                conn.commit()
                mark_repair_complete(PROVIDER, str(arnum))
                clear_item_failure(PROVIDER, str(arnum))
                ok += 1
                guard.success()
                print(f"OK ({len(data)//1024} KB)")
            else:
                fail += 1
                failure_reason = "access_or_format"
                failure_details = {}
                if isinstance(data, tuple) and data and data[0] == "FAILED":
                    if len(data) > 1:
                        failure_reason = str(data[1])
                    if len(data) > 2 and isinstance(data[2], dict):
                        failure_details = data[2]
                retry_after, should_stop, _ = record_failure_and_maybe_diagnose(
                    guard,
                    driver,
                    article_number=arnum,
                    stored_url=stored_url,
                    failure_reason=failure_reason,
                    failure_details=failure_details,
                )
                print(
                    f"실패({failure_reason}; "
                    f"재시도 {retry_after.astimezone().strftime('%Y-%m-%d %H:%M')})"
                )
                if should_stop:
                    break

                if stop_after_item:
                    break

            pace_between_items(
                guard,
                args,
                done=ok + skip + fail,
                index=i,
                total=len(targets),
            )
    except KeyboardInterrupt:
        print("\n[*] 사용자 중단.")
        interrupted = True
    finally:
        driver.quit()
        conn.close()
        guard.close()

    print(f"\n[완료] 성공 {ok} / 건너뜀 {skip} / 실패 {fail}")
    if fail and ok == 0:
        print("[!] 전부 실패 — 로그인 또는 기관 구독 접근 권한을 확인하세요.")
    return download_result_code(ok, fail, interrupted=interrupted)


if __name__ == "__main__":
    raise SystemExit(main())
