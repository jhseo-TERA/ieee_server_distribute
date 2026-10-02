# -*- coding: utf-8 -*-
"""
IEEE 저널(JSSC/TCAS-I/TCAS-II) 최근 구간 메타데이터 스윕 (연세대 프록시).

주기 업데이트용 스크립트 — 기본으로 "올해~작년" 구간만 훑어서
새로 실린 논문을 짧은 시간에 감지합니다. py_00_src/proxy_access_test_v17.py
(최초 전체 스윕용, 2006~2026 전체 대상)을 기반으로 하되:
  - --headless 옵션 추가 (무인 스케줄 실행용)
  - --start-year / --end-year 로 스윕 범위 조절 (기본: 올해 ~ 작년)
  - 로그를 파일로 리다이렉트해도 깨지지 않도록 UTF-8 출력 강제

결과는 기존과 동일하게 py_01_data/00_metadata/*.xlsx 에 저장되고,
이후 scripts/import_excel_to_db.py 로 DB에 upsert 됩니다.

사용:
  .venv\\Scripts\\python.exe scripts\\sweep_ieee_journals.py --headless
  .venv\\Scripts\\python.exe scripts\\sweep_ieee_journals.py --start-year 2026 --end-year 2024 --headless
"""
import os
import sys
import time
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

DEFAULT_JOURNALS = [dict(item, id=item['ieee_id']) for item in sources(kind='journal') if item.get('ieee_id')]


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


def sweep_ieee_journals(start_year, end_year, headless, journals=None):
    if journals is None:
        journals = DEFAULT_JOURNALS
    start_year, end_year = max(start_year, end_year), min(start_year, end_year)
    journals = [item for item in journals if publication_range(item, start_year, end_year)]
    report = BrowserRun('journals', [item['name'] for item in journals])

    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    journal_names_str = "_".join(j["name"] for j in journals)
    file_name = f"IEEE_Sweep_{start_year}_{end_year}_{journal_names_str}_{now_str}.xlsx"
    output_path = os.path.join(ROOT, "py_01_data", "00_metadata", file_name)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    proxy_prefix = "https://access.yonsei.ac.kr/link.n2s?url="
    all_data = []
    driver = make_driver(headless)
    wait = WebDriverWait(driver, 25)

    current_j = "N/A"
    current_y = "N/A"
    current_i = "N/A"

    try:
        print(f"[로그인] 대상 구간: {start_year}~{end_year}")
        driver.get(f"{proxy_prefix}https://ieeexplore.ieee.org/xpl/issues?punumber=4")
        wait.until(EC.presence_of_element_located((By.ID, "id"))).send_keys(os.getenv("YONSEI_ID"))
        driver.find_element(By.ID, "password").send_keys(os.getenv("YONSEI_PW"))
        driver.find_element(By.XPATH, "//input[@type='submit' and @value='로그인']").click()
        time.sleep(5)
        verify_ieee_session(driver, wait)

        for journal in journals:
            current_j = journal["name"]
            print(f"\n[저널] {current_j} 스캔 시작")

            for year in range(start_year, end_year - 1, -1):
                current_y = year
                current_i = "연도 탭 진입 중"

                year_url = f"{proxy_prefix}https://ieeexplore.ieee.org/xpl/issues?punumber={journal['id']}&isyear={year}"
                year_success = False
                issue_info = []

                for year_attempt in range(1, 4):
                    try:
                        driver.get(year_url)
                        if year_attempt > 1:
                            driver.refresh()
                            time.sleep(year_attempt * 5)

                        decade_str = f"{str(year // 10 * 10)}s"
                        decade_xpath = (
                            "//div[contains(@class, 'issue-details-past-tabs') and "
                            f"not(contains(@class, 'year'))]//a[text()='{decade_str}']"
                        )
                        try:
                            decade_button = wait.until(EC.presence_of_element_located((By.XPATH, decade_xpath)))
                            decade_li = decade_button.find_element(By.XPATH, "./parent::li")
                            if "active" not in decade_li.get_attribute("class"):
                                driver.execute_script("arguments[0].click();", decade_button)
                                wait.until(lambda d: "active" in decade_li.get_attribute("class"))
                                time.sleep(2)
                        except Exception:
                            pass

                        year_xpath = f"//div[contains(@class, 'year')]//a[text()='{year}']"
                        year_button = wait.until(EC.element_to_be_clickable((By.XPATH, year_xpath)))
                        year_li = year_button.find_element(By.XPATH, "./parent::li")

                        if "active" not in year_li.get_attribute("class"):
                            driver.execute_script("arguments[0].click();", year_button)
                            wait.until(lambda d: "active" in year_li.get_attribute("class"))
                            time.sleep(3)

                        wait.until(EC.presence_of_element_located((By.PARTIAL_LINK_TEXT, "Issue ")))
                        issue_elements = driver.find_elements(By.PARTIAL_LINK_TEXT, "Issue ")
                        issue_info = [{"text": el.text, "url": el.get_attribute("href")} for el in issue_elements]

                        if issue_info:
                            year_success = True
                            break
                    except Exception as exc:
                        if year_attempt == 3:
                            report.fail(driver, current_j, f'year-{year}-issues', exc)
                        else:
                            time.sleep(year_attempt * 10)

                if not year_success:
                    continue

                for info in issue_info:
                    current_i = info["text"]
                    paged_url = f"{info['url']}&rowsPerPage=100"

                    for attempt in range(1, 4):
                        try:
                            print(f"  {current_j} {current_y} {current_i} ({attempt}/3)...", end=" ", flush=True)
                            driver.get(paged_url)
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
                                            "Journal": current_j, "Year": current_y, "Issue": current_i,
                                            "Title": title_text, "Authors": author_text, "URL": link_url,
                                        })
                                        count += 1
                                except Exception:
                                    continue
                            print(f"OK {count}건")
                            if not count:
                                raise RuntimeError('Issue page contains no parseable paper rows')
                            break
                        except Exception as exc:
                            if attempt == 3:
                                report.fail(driver, current_j, f'{year}-{current_i}', exc)
                            if attempt < 3:
                                driver.refresh()
                                time.sleep(10)
                            continue

                    driver.get(year_url)

                if all_data:
                    pd.DataFrame(all_data).to_excel(output_path, index=False)

        report.finish(all_data)
        print(f"\n[완료] 저널 스윕 종료. 파일: {output_path} (수집 {len(all_data)}건)")
        return output_path

    except Exception as e:
        print("\n" + "=" * 50)
        print("[오류] 치명적 오류 발생 위치")
        print(f"  저널: {current_j} / 연도: {current_y} / 이슈: {current_i}")
        print(f"  내용: {e}")
        print("=" * 50)
        report.fail(driver, current_j, f'{current_y}-{current_i}', e)
        try:
            report.finish(all_data)
        finally:
            raise
    finally:
        driver.quit()


def main():
    now_year = datetime.now().year
    ap = argparse.ArgumentParser(description="IEEE 저널 최근 구간 메타데이터 스윕")
    ap.add_argument("--start-year", type=int, default=now_year, help=f"시작 연도(최신, 기본 {now_year})")
    ap.add_argument("--end-year", type=int, default=now_year - 1, help=f"종료 연도(과거, 기본 {now_year - 1})")
    ap.add_argument("--headless", action="store_true", help="브라우저 창 숨김 (무인 실행용)")
    ap.add_argument("--journals", help="레거시 브라우저 진단 대상 (예: JSSC,TMTT). 주간 수집은 Crossref 사용")
    args = ap.parse_args()

    journals = None
    if args.journals:
        names = {n.strip() for n in args.journals.split(",") if n.strip()}
        journals = [j for j in DEFAULT_JOURNALS if j["name"] in names]
        missing = names - {j["name"] for j in journals}
        if missing:
            sys.exit(f"[!] DEFAULT_JOURNALS에 없는 이름: {missing}")

    sweep_ieee_journals(args.start_year, args.end_year, args.headless, journals=journals)


if __name__ == "__main__":
    main()
