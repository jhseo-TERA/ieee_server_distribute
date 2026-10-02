import os
import time
import pandas as pd
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from datetime import datetime  # 상단 임포트 추가

load_dotenv()

def clean_text(text):
    if not text: return ""
    text = text.replace('\n', ' ').replace('\r', ' ')
    return ' '.join(text.split())

def sweep_ieee_target_range(start_year=2026, end_year=2006, journals=None):
    if journals is None:
        journals = [
            {"name": "JSSC", "id": "4"},
            {"name": "TCAS-I", "id": "8919"},
            {"name": "TCAS-II", "id": "8920"}
        ]
    
    # 타임스탬프 생성
    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    journal_names_str = "_".join([j['name'] for j in journals])
    
    # 날짜/시간이 포함된 파일명
    file_name = f"IEEE_Sweep_{start_year}_{end_year}_{journal_names_str}_{now_str}.xlsx"
    
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
        print(f"🔐 로그인 중... (대상: {start_year}~{end_year})")
        driver.get(f"{proxy_prefix}https://ieeexplore.ieee.org/xpl/issues?punumber=4")
        wait.until(EC.presence_of_element_located((By.ID, "id"))).send_keys(os.getenv("YONSEI_ID"))
        driver.find_element(By.ID, "password").send_keys(os.getenv("YONSEI_PW"))
        driver.find_element(By.XPATH, "//input[@type='submit' and @value='로그인']").click()
        time.sleep(5)

        for journal in journals:
            print(f"\n📘 [{journal['name']}] 스캔 시작")
            
            for year in range(start_year, end_year - 1, -1):
                year_url = f"{proxy_prefix}https://ieeexplore.ieee.org/xpl/issues?punumber={journal['id']}&isyear={year}"
                
                # --- [지능형 재시도 루프 시작] ---
                year_success = False
                issue_info = []

                for year_attempt in range(1, 4): # 최대 3번 시도
                    try:
                        driver.get(year_url)
                        
                        # 2차 시도부터는 새로고침 수행
                        if year_attempt > 1:
                            print(f"🔄 {year}년 로딩 재시도 중... (새로고침 수행)")
                            driver.refresh()
                            time.sleep(year_attempt * 5) # 시도 횟수에 비례해 대기 시간 증가

                        # [STEP 2] Decade(10년 단위) 탭 처리
                        decade_str = f"{str(year // 10 * 10)}s"
                        decade_xpath = f"//div[contains(@class, 'issue-details-past-tabs') and not(contains(@class, 'year'))]//a[text()='{decade_str}']"
                        
                        try:
                            decade_button = wait.until(EC.presence_of_element_located((By.XPATH, decade_xpath)))
                            decade_li = decade_button.find_element(By.XPATH, "./parent::li")
                            if "active" not in decade_li.get_attribute("class"):
                                driver.execute_script("arguments[0].click();", decade_button)
                                wait.until(lambda d: "active" in decade_li.get_attribute("class"))
                                time.sleep(2)
                        except: pass

                        # [STEP 3] Year(세부 연도) 탭 처리
                        year_xpath = f"//div[contains(@class, 'year')]//a[text()='{year}']"
                        year_button = wait.until(EC.element_to_be_clickable((By.XPATH, year_xpath)))
                        year_li = year_button.find_element(By.XPATH, "./parent::li")

                        if "active" not in year_li.get_attribute("class"):
                            driver.execute_script("arguments[0].click();", year_button)
                            wait.until(lambda d: "active" in year_li.get_attribute("class"))
                            time.sleep(3)

                        # [STEP 4] 이슈 목록 확보
                        wait.until(EC.presence_of_element_located((By.PARTIAL_LINK_TEXT, "Issue ")))
                        issue_elements = driver.find_elements(By.PARTIAL_LINK_TEXT, "Issue ")
                        issue_info = [{"text": el.text, "url": el.get_attribute("href")} for el in issue_elements]
                        
                        if issue_info:
                            print(f"✅ {year}년 활성화 성공 (이슈 {len(issue_info)}개 확보)")
                            year_success = True
                            break # 시도 루프 탈출
                            
                    except Exception as e:
                        wait_sec = year_attempt * 10
                        print(f"⚠️ {year}년 {year_attempt}차 시도 실패. ({wait_sec}초 대기)")
                        time.sleep(wait_sec)

                if not year_success:
                    print(f"❌ {year}년 최종 수집 실패. 다음 연도로 건너뜁니다.")
                    continue
                # --- [지능형 재시도 루프 종료] ---

                # [STEP 5] 이슈별 논문 수집 로직 (기존 유지)
                for info in issue_info:
                    issue_text = info["text"]
                    paged_url = f"{info['url']}&rowsPerPage=100"
                    
                    for attempt in range(1, 4):
                        try:
                            print(f"🔎 {journal['name']} {year} {issue_text} ({attempt}/3)...", end=" ", flush=True)
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
                            if attempt < 3:
                                driver.refresh()
                                time.sleep(10)
                            continue

                    driver.get(year_url)
                
                if all_data:
                    pd.DataFrame(all_data).to_excel(output_path, index=False)

        print(f"\n✨ 수집 완료! 최종 파일: {output_path}")

    except Exception as e: print(f"🔥 치명적 오류: {e}")
    finally: driver.quit()

if __name__ == "__main__":
    sweep_ieee_target_range(2026, 2006)