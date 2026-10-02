# -*- coding: utf-8 -*-
"""
IEEE PDF 다운로더 (연세대 프록시) — 즐겨찾기(★) 전용

흐름:
  1) Selenium(Edge)으로 연세대 프록시 로그인 (기존 v16/v21 과 동일한 방식)
  2) 로그인 세션의 쿠키를 requests.Session 으로 이식
  3) DB에서 is_favorite=1 이고 pdf_available=0 이고 article_number 가 숫자(IEEE)인
     논문을 골라 프록시된 stamp.jsp / getPDF.jsp URL 로 PDF 다운로드
  4) %PDF 검증 후 ieee-pdf/<article_number>.pdf 저장, DB(pdf_available/pdf_local_path) 갱신

이 스크립트는 일간 PDF routine의 내부 단계로만 실행되며, 항상 즐겨찾기(★)
등록된 논문만 대상으로 합니다. 전체 다운로드는 지원하지 않습니다.
Optica(비숫자 키)는 stamp.jsp 방식이 아니므로 이 스크립트 대상에서 제외됩니다.

사용 예:
  # 즐겨찾기 중 최신순 20건만 테스트 다운로드
  .venv\\Scripts\\python.exe scripts\\download_pdfs.py

  .venv\\Scripts\\python.exe scripts\\run_pdf_download_routine.py --provider ieee --limit 20
"""
import os
import re
import sys
import time
import random
import argparse
from urllib.parse import urljoin, urlparse

import pymysql
from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.edge.options import Options

try:
    from .pdf_proxy_auth import authenticate, verify_publisher, require_credentials, bootstrap_failure
    from .download_guard import (
        DownloadGuard,
        add_guard_arguments,
        bounded_download_limit,
        download_result_code,
        invoked_by_daily_routine,
        load_repair_ids,
        mark_repair_complete,
        retry_after_seconds,
        validate_pdf_file,
    )
except ImportError:  # Direct execution from the scripts directory.
    from pdf_proxy_auth import authenticate, verify_publisher, require_credentials, bootstrap_failure
    from download_guard import (
        DownloadGuard,
        add_guard_arguments,
        bounded_download_limit,
        download_result_code,
        invoked_by_daily_routine,
        load_repair_ids,
        mark_repair_complete,
        retry_after_seconds,
        validate_pdf_file,
    )

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---------- 경로/환경 ----------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PDF_DIR = os.path.join(ROOT, "ieee-pdf")   # IEEE(숫자 키) PDF 저장 폴더
os.makedirs(PDF_DIR, exist_ok=True)
load_dotenv(os.path.join(ROOT, ".env"))

PROXY_PREFIX = "https://access.yonsei.ac.kr/link.n2s?url="
DEFAULT_HOST = "ieeexplore-ieee-org-ssl.access.yonsei.ac.kr"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 Edg/124.0")

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
    """다운로드 대상(즐겨찾기 등록 + IEEE + pdf 미보유) 조회."""
    repair_ids = load_repair_ids("ieee")
    where = ["pdf_available = 0", "source_system = 'ieee'"]
    params = []
    if repair_ids:
        placeholders = ",".join(["%s"] * len(repair_ids))
        where.append(f"(is_favorite = 1 OR article_number IN ({placeholders}))")
        params.extend(repair_ids)
    else:
        where.append("is_favorite = 1")
    if args.source:
        where.append("source_name = %s")
        params.append(args.source)
    if args.type:
        where.append("source_type = %s")
        params.append(args.type)
    if args.year:
        where.append("year = %s")
        params.append(str(args.year))
    if args.min_year:
        where.append("(year+0) >= %s")
        params.append(int(args.min_year))
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


def selenium_login(headless=False):
    """프록시 로그인 후 driver 반환 (쿠키 확보용)."""
    require_credentials()

    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1200,900")
    print("[*] Edge 브라우저 기동...")
    driver = webdriver.Edge(options=options)
    try:
        host = authenticate(driver, "ieee", PROXY_PREFIX + "https://ieeexplore.ieee.org/xpl/issues?punumber=4")
        print(f"[*] IEEE 로그인 검증 완료: {host}")
        return driver
    except BaseException:
        try:
            driver.quit()
        except Exception:
            pass
        raise


def build_session(driver, sample_url=None):
    """driver 쿠키를 requests.Session 으로 이식하고 프록시 host를 유지."""
    import requests
    current_host = verify_publisher(driver, "ieee")
    # DB의 url은 대개 원본 ieeexplore.ieee.org 주소다. 이를 사용하면 연세대
    # 프록시 로그인 직후에도 직접 호스트로 빠져 기관 인증 세션을 잃는다.
    host = current_host

    # 프록시된 IEEE 호스트의 쿠키를 확보하기 위해 해당 호스트로 이동
    driver.get(f"https://{host}/Xplore/home.jsp")
    host = verify_publisher(driver, "ieee")

    sess = requests.Session()
    sess.headers.update({"User-Agent": driver.execute_script("return navigator.userAgent") or UA,
                         "Accept-Language": "ko,en;q=0.9"})
    n = 0
    for c in driver.get_cookies():
        try:
            sess.cookies.set(c["name"], c["value"],
                             domain=c.get("domain"), path=c.get("path", "/"))
            n += 1
        except Exception:
            continue
    print(f"[*] 쿠키 {n}개 이식 완료 (host={host})")
    return sess, host


def looks_like_pdf(resp):
    ct = (resp.headers.get("Content-Type") or "").lower()
    if "application/pdf" in ct:
        return True
    # content-type 이 애매하면 매직바이트로 판별
    head = resp.content[:1024]
    return head.startswith(b"%PDF")


IEEE_BLOCK_MARKERS = ("unable to load page", "member_profile_change_usernamepass_link")


def looks_like_blocked(resp) -> bool:
    """IEEE Xplore의 IP 레벨 자동화 차단(HTTP 418 / 'Unable to Load Page') 감지.
    2026-07 대량 다운로드 후 실제로 겪은 차단 패턴 - 이 상태에서 계속 요청해봐야
    전부 실패만 하고 차단만 연장될 수 있어, 만나면 즉시 런 전체를 중단시킨다."""
    if getattr(resp, "status_code", None) == 418:
        return True
    text = (resp.text or "").lower() if hasattr(resp, "text") else ""
    return any(m in text for m in IEEE_BLOCK_MARKERS)


def download_one(sess, host, arnum):
    """Return PDF bytes, None, RELOGIN, or (BLOCKED, cooldown_seconds)."""
    stamp = f"https://{host}/stamp/stamp.jsp?tp=&arnumber={arnum}"
    getpdf = f"https://{host}/stampPDF/getPDF.jsp?tp=&arnumber={arnum}&ref="
    hdr = {"Referer": stamp, "Accept": "application/pdf,*/*"}

    # 1차: getPDF 직접
    try:
        r = sess.get(getpdf, headers=hdr, timeout=90, allow_redirects=True)
        if looks_like_pdf(r):
            return r.content
        if looks_like_blocked(r) or r.status_code in (403, 429, 503):
            return ("BLOCKED", retry_after_seconds(r, 12 * 3600))
    except Exception:
        r = None

    # 2차: stamp 페이지의 iframe src 파싱 후 재요청
    try:
        r2 = sess.get(stamp, headers={"Referer": f"https://{host}/document/{arnum}/"},
                      timeout=60, allow_redirects=True)
        if looks_like_blocked(r2) or r2.status_code in (403, 429, 503):
            return ("BLOCKED", retry_after_seconds(r2, 12 * 3600))
        html = r2.text
        m = (re.search(r'<(?:iframe|frame)[^>]+src="([^"]+getPDF[^"]*)"', html, re.I)
             or re.search(r'src="([^"]*\.pdf[^"]*)"', html, re.I)
             or re.search(r'"(https?://[^"]*/ielx[^"]+\.pdf[^"]*)"', html, re.I))
        if m:
            src = urljoin(stamp, m.group(1).replace("&amp;", "&"))
            r3 = sess.get(src, headers=hdr, timeout=90, allow_redirects=True)
            if looks_like_pdf(r3):
                return r3.content
            if looks_like_blocked(r3) or r3.status_code in (403, 429, 503):
                return ("BLOCKED", retry_after_seconds(r3, 12 * 3600))
        # 로그인 페이지로 튕겼는지 감지
        if "access.yonsei.ac.kr" in r2.url and ("password" in html or "로그인" in html):
            return "RELOGIN"
    except Exception:
        pass
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="IEEE PDF 다운로더 (연세대 프록시) — 즐겨찾기(★) 전용")
    ap.add_argument(
        "--limit", type=bounded_download_limit, default=30,
        help="최대 다운로드 수 (1~30, 기본 30)",
    )
    ap.add_argument("--source", help="특정 출처만 (예: JSSC, ISSCC)")
    ap.add_argument("--type", choices=["journal", "conference"], help="구분")
    ap.add_argument("--year", help="특정 연도")
    ap.add_argument("--min-year", dest="min_year", help="이 연도 이상")
    add_guard_arguments(ap, min_delay=30.0, max_delay=60.0)
    ap.add_argument("--batch-size", dest="batch_size", type=int, default=15,
                     help="이 건수마다 배치 휴식 삽입 (기본 15, 0이면 비활성)")
    ap.add_argument("--batch-pause-min", dest="batch_pause_min", type=float, default=60.0,
                     help="배치 휴식 최소 초 (기본 60)")
    ap.add_argument("--batch-pause-max", dest="batch_pause_max", type=float, default=120.0,
                     help="배치 휴식 최대 초 (기본 120)")
    ap.add_argument("--headless", action="store_true", help="브라우저 창 숨김")
    args = ap.parse_args()
    if not invoked_by_daily_routine():
        print("[실패] 개별 다운로더 직접 실행은 차단되어 있습니다. 일간 PDF routine을 사용하세요.")
        return 2

    conn = db_connect()
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
        "ieee",
        min_delay=args.min_sleep,
        max_delay=args.max_sleep,
        cooldown_hours=args.cooldown_hours,
        max_consecutive_failures=args.max_consecutive_failures,
    )
    allowed, reason = guard.acquire()
    if not allowed:
        print(f"[*] IEEE 실행 생략: {reason}")
        print("[완료] 성공 0 / 건너뜀 0 / 실패 0")
        conn.close()
        return 0

    interrupted = False
    driver = None
    try:
        driver = selenium_login(headless=args.headless)
        sess, host = build_session(driver, targets[0][1])
    except Exception as exc:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        guard.close()
        conn.close()
        return bootstrap_failure("ieee", exc)

    ok = skip = fail = 0
    upd = conn.cursor()
    try:
        for i, (arnum, url, src, year, title) in enumerate(targets, 1):
            # DB 원본 URL이 아니라 로그인으로 확정된 연세대 프록시 host를 사용한다.
            url_host = urlparse(url or "").netloc
            row_host = url_host if url_host.endswith(".access.yonsei.ac.kr") else host
            out = os.path.join(PDF_DIR, f"{arnum}.pdf")

            if os.path.isfile(out) and os.path.getsize(out) > 2048:
                valid, _ = validate_pdf_file(out)
                if valid:
                    rel = os.path.relpath(out, ROOT).replace("\\", "/")
                    upd.execute("UPDATE papers SET pdf_available=1, pdf_local_path=%s WHERE article_number=%s",
                                (rel, str(arnum)))
                    conn.commit()
                    mark_repair_complete("ieee", str(arnum))
                    skip += 1
                    continue

            print(f"  [{i}/{len(targets)}] {src} {year} #{arnum} … ", end="", flush=True)
            data = download_one(sess, row_host, arnum)

            if data == "RELOGIN":
                print("세션 만료 감지 → 재로그인 시도")
                try:
                    authenticate(driver, "ieee", PROXY_PREFIX + "https://ieeexplore.ieee.org/xpl/issues?punumber=4")
                    sess, host = build_session(driver, url or f"https://{DEFAULT_HOST}/")
                except Exception as exc:
                    # Preserve the final counters so the routine can retain
                    # earlier successes and release only unused reservations.
                    fail += 1
                    print(f"실패(세션 재인증: {type(exc).__name__})")
                    guard.failure()
                    break
                data = download_one(sess, host, arnum)

            if isinstance(data, tuple) and data[0] == "BLOCKED":
                print("차단 감지(HTTP 418 / Unable to Load Page)")
                fail += 1
                guard.cooldown("IEEE 차단 또는 속도제한 감지", seconds=data[1])
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
                    print(f"실패(PDF 구조 오류: {validation_error})")
                    if guard.failure():
                        break
                    continue
                os.replace(temporary_out, out)
                rel = os.path.relpath(out, ROOT).replace("\\", "/")
                upd.execute(
                    "UPDATE papers SET pdf_available=1, pdf_local_path=%s "
                    "WHERE article_number=%s", (rel, str(arnum)))
                conn.commit()
                mark_repair_complete("ieee", str(arnum))
                ok += 1
                guard.success()
                print(f"OK ({len(data)//1024} KB)")
            else:
                fail += 1
                print("실패(접근권한/형식)")
                if guard.failure():
                    break

            done = ok + skip + fail
            if args.batch_size > 0 and done % args.batch_size == 0 and i < len(targets):
                pause = random.uniform(args.batch_pause_min, args.batch_pause_max)
                print(f"    [배치 휴식] {done}건 처리 -> {pause:.0f}초 대기...")
                time.sleep(pause)
            else:
                guard.pace()
    except KeyboardInterrupt:
        print("\n[*] 사용자 중단.")
        interrupted = True
    finally:
        try:
            driver.quit()
        except Exception as exc:
            # Browser teardown must not hide committed PDF results.
            print(f"[주의] IEEE 브라우저 종료 오류: {type(exc).__name__}")
        conn.close()
        guard.close()

    print(f"\n[완료] 성공 {ok} / 건너뜀 {skip} / 실패 {fail}")
    if fail and ok == 0:
        print("[!] 전부 실패 — 프록시 로그인 또는 기관 구독 접근 권한을 확인하세요.")
    return download_result_code(ok, fail, interrupted=interrupted)


if __name__ == "__main__":
    raise SystemExit(main())
