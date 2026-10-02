import os
import time
import re
import pandas as pd
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

load_dotenv()

def clean_text(text):
    """줄바꿈 및 불필요한 연속 공백 제거 함수"""
    if not text: return ""
    # \n, \r 제거 후 공백으로 대체
    text = text.replace('\n', ' ').replace('\r', ' ')
    # 연속된 공백을 하나로 합치고 양끝 공백 제거
    return ' '.join(text.split())

def sweep_ieee_target_range(start_year=2026, end_year=2006, journals=None):
    # 1. 저널 설정 및 파일명 자동 생성
    if journals is None:
        journals = [
            {"name": "JSSC", "id": "4"},
            {"name": "TCAS-I", "id": "8919"},
            {"name": "TCAS-II", "id": "8920"}
        ]
    
    journal_names_str = "_".join([j['name'] for j in journals])
    file_name = f"IEEE_Sweep_{start_year}_{end_year}_{journal_names_str}.xlsx"
    
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    output_path = os.path.join(base_dir, "py_01_data", "00_metadata", file_name)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    proxy_prefix = "https://access.yonsei.ac.kr/link.n2s?url="
    all_data = []
    options = Options()
    driver = webdriver.Edge(options=options)
    wait = WebDriverWait(driver, 25)

    try:
        # [STEP 1] 로그인
        print(f"🔐 로그인 중... (저장파일명: {file_name})")
        driver.get(f"{proxy_prefix}https://ieeexplore.ieee.org/xpl/issues?punumber=4")
        wait.until(EC.presence_of_element_located((By.ID, "id"))).send_keys(os.getenv("YONSEI_ID"))
        driver.find_element(By.ID, "password").send_keys(os.getenv("YONSEI_PW"))
        driver.find_element(By.XPATH, "//input[@type='submit' and @value='로그인']").click()
        time.sleep(5)

        for journal in journals:
            print(f"\n📘 [{journal['name']}] 스캔 시작")
            
            for year in range(start_year, end_year - 1, -1):
                year_url = f"{proxy_prefix}https://ieeexplore.ieee.org/xpl/issues?punumber={journal['id']}&isyear={year}"
                driver.get(year_url)
                
                try:
                    # [연도 탭 활성화 확인]
                    year_xpath = f"//a[text()='{year}' and contains(@data-analytics_identifier, 'year')]"
                    year_button = wait.until(EC.presence_of_element_located((By.XPATH, year_xpath)))
                    parent_li = year_button.find_element(By.XPATH, "./parent::li")
                    
                    if "active" not in parent_li.get_attribute("class"):
                        driver.execute_script("arguments[0].click();", year_button)
                        wait.until(lambda d: "active" in parent_li.get_attribute("class"))
                        time.sleep(3)

                    issue_elements = driver.find_elements(By.PARTIAL_LINK_TEXT, "Issue ")
                    issue_info = [{"text": el.text, "url": el.get_attribute("href")} for el in issue_elements]
                except: continue

                for info in issue_info:
                    issue_text = info["text"]
                    paged_url = f"{info['url']}&rowsPerPage=100" # 100개씩 보기 적용
                    
                    for attempt in range(3): # 3회 재시도 로직
                        try:
                            print(f"🔎 {journal['name']} {year} {issue_text} (시도 {attempt+1}/3)...", end=" ", flush=True)
                            driver.get(paged_url)
                            wait.until(EC.presence_of_element_located((By.CLASS_NAME, "List-results-items")))
                            time.sleep(2)

                            paper_items = driver.find_elements(By.CLASS_NAME, "List-results-items")
                            count = 0
                            for item in paper_items:
                                try:
                                    title_el = item.find_element(By.CSS_SELECTOR, "a[xplmathjax]")
                                    # 데이터 수집 시 클리닝 적용
                                    title_text = clean_text(title_el.text)
                                    link_url = title_el.get_attribute("href")
                                    
                                    try:
                                        author_text = clean_text(item.find_element(By.CSS_SELECTOR, "p.author").text)
                                    except: author_text = "N/A"

                                    if title_text and "/document/" in link_url:
                                        all_data.append({
                                            "Journal": journal['name'], "Year": year, "Issue": issue_text,
                                            "Title": title_text, "Authors": author_text, "URL": link_url
                                        })
                                        count += 1
                                except: continue
                            print(f"✅ {count}개")
                            break
                        except:
                            time.sleep(5)
                            continue

                    driver.get(year_url)
                
                # 연도별 중간 저장
                if all_data:
                    pd.DataFrame(all_data).to_excel(output_path, index=False)

        print(f"\n✨ 전수 조사 완료! 최종 경로: {output_path}")

    except Exception as e: print(f"🔥 오류: {e}")
    finally: driver.quit()

if __name__ == "__main__":
    target_journals = [
        {"name": "JSSC", "id": "4"},
        {"name": "TCAS-I", "id": "8919"},
        {"name": "TCAS-II", "id": "8920"}
    ]
    sweep_ieee_target_range(2026, 2020, journals=target_journals)