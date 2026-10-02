# -*- coding: utf-8 -*-
"""
Nature Photonics / Nature Communications PDF 다운로더 (연세대 프록시) — 즐겨찾기(★) 전용

IEEE용 download_pdfs.py와 동일한 구조(로그인 1번 -> 쿠키를 requests.Session으로
이식 -> 이후 논문마다 순수 requests 요청)를 그대로 씀. 조사 결과 Nature.com은
Optica처럼 논문마다 브라우저 navigate(JS 챌린지 통과)가 필요 없고, PDF 링크가
그냥 <기사 URL>.pdf 로 고정 패턴이라 IEEE 방식이 그대로 통함(Nature
Communications는 오픈액세스라 원래 로그인 없이도 되지만, 요청대로 두 저널 다
동일하게 연세대 프록시를 거치도록 통일).

흐름:
  1) Selenium(Edge)으로 연세대 프록시 로그인
  2) DB에서 is_favorite=1, pdf_available=0, source_name이 NPHOTON/NCOMMS인
     논문을 골라 <프록시 호스트>/articles/<키>.pdf 를 requests 로 바로 다운로드
  3) %PDF 검증 후 nature-pdf/<키>.pdf 저장 + DB(pdf_available/pdf_local_path) 갱신

사용 예:
  .venv\\Scripts\\python.exe scripts\\run_pdf_download_routine.py --provider nature --limit 10
"""
import os
import sys
import time
import argparse
from urllib.parse import urlparse

import pymysql
import requests
from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.edge.options import Options

try:
    from .pdf_proxy_auth import authenticate, require_credentials, bootstrap_failure
    from . import download_guard
except ImportError:
    from pdf_proxy_auth import authenticate, require_credentials, bootstrap_failure
    import download_guard

# The same module supports both package imports and direct scheduler execution.
DownloadGuard = download_guard.DownloadGuard
add_guard_arguments = download_guard.add_guard_arguments
bounded_download_limit = download_guard.bounded_download_limit
download_result_code = download_guard.download_result_code
invoked_by_daily_routine = download_guard.invoked_by_daily_routine
load_repair_ids = download_guard.load_repair_ids
mark_repair_complete = download_guard.mark_repair_complete
retry_after_seconds = download_guard.retry_after_seconds
validate_pdf_file = download_guard.validate_pdf_file

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PDF_DIR = os.path.join(ROOT, "nature-pdf")
os.makedirs(PDF_DIR, exist_ok=True)
load_dotenv(os.path.join(ROOT, ".env"))

PROXY_PREFIX = "https://access.yonsei.ac.kr/link.n2s?url="
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

DB = dict(
    host=os.getenv("DB_HOST", "127.0.0.1"),
    port=int(os.getenv("DB_PORT", "3306")),
    user=os.getenv("DB_USER", "root"),
    password=os.getenv("DB_PASSWORD", ""),
    database=os.getenv("DB_NAME", "ieee_repo"),
    charset="utf8mb4",
)

NATURE_SOURCES = ("NPHOTON", "NELECTRON", "NCOMMS")


def db_connect():
    return pymysql.connect(**DB)


def fetch_targets(conn, args):
    """다운로드 대상(즐겨찾기 + Nature + pdf 미보유) 조회."""
    repair_ids = load_repair_ids("nature")
    where = ["pdf_available = 0", "source_system = 'nature'"]
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
    if args.min_year:
        where.append("(year+0) >= %s")
        params.append(int(args.min_year))
    order_sql = ""
    if repair_ids:
        placeholders = ",".join(["%s"] * len(repair_ids))
        order_sql = f"CASE WHEN article_number IN ({placeholders}) THEN 0 ELSE 1 END, "
        params.extend(repair_ids)
    sql = (
        "SELECT article_number, source_name, year, LEFT(title,60) "
        "FROM papers WHERE " + " AND ".join(where) +
        " ORDER BY " + order_sql + "(year+0) DESC, id DESC"
    )
    sql += " LIMIT %s"
    params.append(int(args.limit))
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def selenium_login():
    require_credentials()

    options = Options()
    options.add_argument("--window-size=1200,900")
    print("[*] Edge 브라우저 기동...")
    driver = webdriver.Edge(options=options)
    try:
        host = authenticate(driver, "nature", f"{PROXY_PREFIX}https://www.nature.com/nphoton/")
        print(f"[*] Nature 로그인 검증 완료: {host}")
        return driver
    except BaseException:
        try:
            driver.quit()
        except Exception:
            pass
        raise


def transplant_cookies(driver):
    sess = requests.Session()
    try:
        browser_ua = driver.execute_script("return navigator.userAgent")
    except Exception:
        browser_ua = UA
    sess.headers.update({"User-Agent": browser_ua, "Accept-Language": "ko,en;q=0.9"})
    n = 0
    for c in driver.get_cookies():
        try:
            sess.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
            n += 1
        except Exception:
            continue
    print(f"[*] 쿠키 {n}개 이식 완료")
    return sess


def looks_like_pdf(resp):
    ct = (resp.headers.get("Content-Type") or "").lower()
    if "application/pdf" in ct:
        return True
    return resp.content[:4] == b"%PDF"


def download_one(sess, host, key):
    """Return PDF bytes, None, or (THROTTLED, cooldown_seconds)."""
    pdf_url = f"https://{host}/articles/{key}.pdf"
    article_url = f"https://{host}/articles/{key}"
    hdr = {"Referer": article_url, "Accept": "application/pdf,*/*"}
    try:
        r = sess.get(pdf_url, headers=hdr, timeout=90, allow_redirects=True)
        if looks_like_pdf(r):
            return r.content
        if r.status_code in (403, 418, 429, 503):
            return ("THROTTLED", retry_after_seconds(r, 12 * 3600))
    except Exception:
        pass
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Nature PDF 다운로더 (연세대 프록시) — 즐겨찾기(★) 전용")
    ap.add_argument(
        "--limit", type=bounded_download_limit, default=30,
        help="최대 다운로드 수 (1~30, 기본 30)",
    )
    ap.add_argument("--source", choices=NATURE_SOURCES, help="특정 저널만 (NPHOTON, NELECTRON, NCOMMS)")
    ap.add_argument("--min-year", dest="min_year", help="이 연도 이상")
    add_guard_arguments(ap, min_delay=30.0, max_delay=60.0)
    ap.add_argument("--sleep", type=float, help="호환 옵션: 요청 사이 고정 대기초")
    args = ap.parse_args()
    if not invoked_by_daily_routine():
        print("[실패] 개별 다운로더 직접 실행은 차단되어 있습니다. 일간 PDF routine을 사용하세요.")
        return 2
    if args.sleep is not None:
        args.min_sleep = args.max_sleep = max(0.0, args.sleep)

    conn = db_connect()
    targets = fetch_targets(conn, args)
    if not targets:
        print("[*] 다운로드 대상이 없습니다. (즐겨찾기 중 조건에 맞는 미보유 PDF 없음)")
        print("[완료] 성공 0 / 건너뜀 0 / 실패 0")
        conn.close()
        return 0
    print(f"[*] 대상 {len(targets):,}건 (상위 {args.limit}건)")

    guard = DownloadGuard(
        "nature",
        min_delay=args.min_sleep,
        max_delay=args.max_sleep,
        cooldown_hours=args.cooldown_hours,
        max_consecutive_failures=args.max_consecutive_failures,
    )
    allowed, reason = guard.acquire()
    if not allowed:
        print(f"[*] Nature 실행 생략: {reason}")
        print("[완료] 성공 0 / 건너뜀 0 / 실패 0")
        conn.close()
        return 0

    interrupted = False
    driver = None
    try:
        driver = selenium_login()
        host = urlparse(driver.current_url).netloc or "www-nature-com-ssl.access.yonsei.ac.kr"
        sess = transplant_cookies(driver)
    except Exception as exc:
        guard.close()
        conn.close()
        return bootstrap_failure("nature", exc)
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    ok = skip = fail = 0
    upd_conn = db_connect()
    upd = upd_conn.cursor()
    try:
        for i, (key, src, year, title) in enumerate(targets, 1):
            out = os.path.join(PDF_DIR, f"{key}.pdf")

            if os.path.isfile(out) and os.path.getsize(out) > 2048:
                valid, _ = validate_pdf_file(out)
                if valid:
                    rel = os.path.relpath(out, ROOT).replace("\\", "/")
                    upd.execute("UPDATE papers SET pdf_available=1, pdf_local_path=%s WHERE article_number=%s",
                                (rel, str(key)))
                    upd_conn.commit()
                    mark_repair_complete("nature", str(key))
                    skip += 1
                    continue

            print(f"  [{i}/{len(targets)}] {src} {year} #{key} … ", end="", flush=True)
            data = download_one(sess, host, key)

            if isinstance(data, tuple) and data[0] == "THROTTLED":
                print("속도제한/접근차단 감지")
                fail += 1
                guard.cooldown("Nature 속도제한 또는 접근차단 감지", seconds=data[1])
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
                    "WHERE article_number=%s", (rel, str(key)))
                upd_conn.commit()
                mark_repair_complete("nature", str(key))
                ok += 1
                guard.success()
                print(f"OK ({len(data)//1024} KB)")
            else:
                fail += 1
                print("실패(접근권한/형식)")
                if guard.failure():
                    break

            guard.pace()
    except KeyboardInterrupt:
        print("\n[*] 사용자 중단.")
        interrupted = True
    finally:
        upd_conn.close()
        conn.close()
        guard.close()

    print(f"\n[완료] 성공 {ok} / 건너뜀 {skip} / 실패 {fail}")
    if fail and ok == 0:
        print("[!] 전부 실패 — 로그인 또는 기관 구독 접근 권한을 확인하세요.")
    return download_result_code(ok, fail, interrupted=interrupted)


if __name__ == "__main__":
    raise SystemExit(main())
