# -*- coding: utf-8 -*-
"""
IEEE 학회(ISSCC/VLSI/RFIC 등) 최근 구간 메타데이터 스윕 (연세대 프록시).

CICC는 도서관 프록시를 사용하지 않는 fetch_ieee_crossref.py가 담당합니다.

주기 업데이트용 스크립트 — 기본으로 "올해~작년" 구간만 훑어서
새로 실린 논문을 짧은 시간에 감지합니다. py_00_src/proxy_access_test_v21.py
(최초 전체 스윕용, 2006~2026 전체 대상)을 기반으로 하되:
  - --headless 옵션 추가 (무인 스케줄 실행용)
  - --start-year / --end-year 로 스윕 범위 조절 (기본: 올해 ~ 작년)
  - 로그를 파일로 리다이렉트해도 깨지지 않도록 UTF-8 출력 강제

결과는 기존과 동일하게 py_01_data/00_metadata/*.xlsx 에 저장되고,
이후 scripts/import_excel_to_db.py 로 DB에 upsert 됩니다.

사용:
  .venv\\Scripts\\python.exe scripts\\sweep_ieee_conferences.py --headless
  .venv\\Scripts\\python.exe scripts\\sweep_ieee_conferences.py --start-year 2026 --end-year 2024 --headless
"""
import os
import sys
import time
import re
import math
import argparse
from datetime import datetime

import pandas as pd
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from scripts.metadata_sources import sources, publication_range
from scripts.metadata_health import BrowserRun, verify_ieee_session

DEFAULT_CONFERENCES = [dict(item, id=item['ieee_id']) for item in sources(kind='conference') if item.get('ieee_id')]


def clean_text(text):
    if not text:
        return ""
    text = text.replace("\n", " ").replace("\r", " ")
    return " ".join(text.split())


def make_driver(headless: bool):
    """headless 시 프록시/로그인 페이지의 봇 탐지에 걸려 로그인이 막히는 문제가 있어
    (headful에서는 정상 동작 확인됨), Optica 스윕(v28)과 동일한 탐지 회피 옵션을 적용."""
    options = Options()
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1200,900")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    driver = webdriver.Edge(options=options)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return driver


def sweep_ieee_conferences(start_year, end_year, headless, conferences=None):
    if conferences is None:
        conferences = DEFAULT_CONFERENCES
    start_year, end_year = max(start_year, end_year), min(start_year, end_year)
    conferences = [item for item in conferences if publication_range(item, start_year, end_year)]
    report = BrowserRun('conferences', [item['name'] for item in conferences])

    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = f"IEEE_Conf_Sweep_{start_year}_{end_year}_{now_str}.xlsx"
    output_path = os.path.join(ROOT, "py_01_data", "00_metadata", file_name)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    proxy_prefix = "https://access.yonsei.ac.kr/link.n2s?url="
    all_data = []
    driver = make_driver(headless)
    wait = WebDriverWait(driver, 25)

    current_conf = "N/A"
    current_y = "N/A"

    try:
        print("[로그인] 세션 확보 중...")
        driver.get(f"{proxy_prefix}https://ieeexplore.ieee.org/xpl/conhome/1000708/all-proceedings")
        wait.until(EC.presence_of_element_located((By.ID, "id"))).send_keys(os.getenv("YONSEI_ID"))
        driver.find_element(By.ID, "password").send_keys(os.getenv("YONSEI_PW"))
        driver.find_element(By.XPATH, "//input[@type='submit' and @value='로그인']").click()
        time.sleep(5)
        verify_ieee_session(driver, wait)

        for conf in conferences:
            current_conf = conf["name"]
            print(f"\n[학회] {current_conf} All Proceedings 목록 스캔")
            all_proc_url = f"{proxy_prefix}https://ieeexplore.ieee.org/xpl/conhome/{conf['id']}/all-proceedings"
            driver.get(all_proc_url)

            try:
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "li h2 a")))
                proc_links = driver.find_elements(By.CSS_SELECTOR, "li h2 a")
                target_proceedings = []
                for link in proc_links:
                    txt, url = link.text, link.get_attribute("href")
                    match = re.search(r"20\d{2}", txt)
                    if match:
                        y = int(match.group())
                        if end_year <= y <= start_year:
                            target_proceedings.append({"year": y, "url": url})

                for proc in target_proceedings:
                    current_y = proc["year"]
                    base_proc_url = proc["url"]

                    first_page_url = f"{base_proc_url}?rowsPerPage=100&pageNumber=1"
                    driver.get(first_page_url)

                    total_papers = None
                    try:
                        wait.until(EC.presence_of_element_located((By.CLASS_NAME, "Dashboard-header")))
                        header_el = driver.find_element(By.CLASS_NAME, "Dashboard-header")
                        strong_elements = header_el.find_elements(By.CLASS_NAME, "strong")
                        if len(strong_elements) >= 2:
                            total_papers = int(strong_elements[-1].text.replace(",", ""))
                        else:
                            match = re.search(r"of\s+([\d,]+)", header_el.text)
                            if match:
                                total_papers = int(match.group(1).replace(",", ""))
                    except Exception:
                        print(f"[주의] {current_y}년 파싱 지연... (자바스크립트 재시도)")
                        try:
                            total_papers = int(driver.execute_script(
                                "return document.querySelector('.Dashboard-header')"
                                ".querySelectorAll('.strong')[1].innerText;"
                            ).replace(",", ""))
                        except Exception as exc:
                            raise RuntimeError('Cannot verify proceedings page count') from exc

                    if total_papers is None or total_papers <= 0:
                        raise RuntimeError('Proceedings page count is missing or zero')
                    total_pages = math.ceil(total_papers / 100)
                    print(f"  {current_y}년: 총 {total_papers}개 논문 ({total_pages}페이지) 감지")

                    for page in range(1, total_pages + 1):
                        paged_url = f"{base_proc_url}?rowsPerPage=100&pageNumber={page}"

                        for attempt in range(1, 4):
                            try:
                                print(f"  {current_conf} {current_y} [P{page}/{total_pages}] ({attempt}/3)...", end=" ", flush=True)
                                driver.get(paged_url)
                                if attempt > 1:
                                    driver.refresh()
                                    time.sleep(5)

                                wait.until(EC.presence_of_element_located((By.CLASS_NAME, "List-results-items")))
                                time.sleep(2)

                                paper_items = driver.find_elements(By.CLASS_NAME, "List-results-items")
                                count = 0
                                for item in paper_items:
                                    try:
                                        title_el = item.find_element(By.CSS_SELECTOR, "a[xplmathjax]")
                                        title_text = clean_text(title_el.text)
                                        link_url = title_el.get_attribute("href")
                                        try:
                                            author_text = clean_text(item.find_element(By.CSS_SELECTOR, "p.author").text)
                                        except Exception:
                                            author_text = "N/A"

                                        if title_text and "/document/" in link_url:
                                            all_data.append({
                                                "Conference": current_conf, "Year": current_y,
                                                "Page": page, "Title": title_text, "Authors": author_text, "URL": link_url,
                                            })
                                            count += 1
                                    except Exception:
                                        continue

                                print(f"OK {count}건")
                                if not count:
                                    raise RuntimeError('Page contains no parseable paper rows')
                                break
                            except Exception as exc:
                                if attempt == 3:
                                    report.fail(driver, current_conf, f'{current_y}-page-{page}', exc)
                                else:
                                    time.sleep(10)

                    if all_data:
                        pd.DataFrame(all_data).to_excel(output_path, index=False)

            except Exception as e:
                report.fail(driver, current_conf, 'proceedings-list', e)
                continue

        report.finish(all_data)
        print(f"\n[완료] 학회 스윕 종료. 파일: {output_path} (수집 {len(all_data)}건)")
        return output_path

    except Exception as e:
        print(f"\n[오류] 치명적 오류 발생 위치: {current_conf}/{current_y}")
        print(f"  내용: {e}")
        report.fail(driver, current_conf, f'{current_y}-fatal', e)
        try:
            report.finish(all_data)
        finally:
            raise
    finally:
        driver.quit()


def main():
    now_year = datetime.now().year
    ap = argparse.ArgumentParser(description="IEEE 학회 최근 구간 메타데이터 스윕")
    ap.add_argument("--start-year", type=int, default=now_year, help=f"시작 연도(최신, 기본 {now_year})")
    ap.add_argument("--end-year", type=int, default=now_year - 1, help=f"종료 연도(과거, 기본 {now_year - 1})")
    ap.add_argument("--headless", action="store_true", help="브라우저 창 숨김 (무인 실행용)")
    ap.add_argument("--conferences", help="쉼표로 구분된 학회명만 대상으로 (예: BCICTS). 미지정시 DEFAULT_CONFERENCES 전체")
    args = ap.parse_args()

    conferences = None
    if args.conferences:
        names = {n.strip() for n in args.conferences.split(",") if n.strip()}
        conferences = [c for c in DEFAULT_CONFERENCES if c["name"] in names]
        missing = names - {c["name"] for c in conferences}
        if missing:
            sys.exit(f"[!] DEFAULT_CONFERENCES에 없는 이름: {missing}")

    sweep_ieee_conferences(args.start_year, args.end_year, args.headless, conferences=conferences)


if __name__ == "__main__":
    main()
