import os
import time
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
    text = text.replace('\n', ' ').replace('\r', ' ')
    return ' '.join(text.split())

def sweep_ieee_target_range(start_year=2026, end_year=2006, journals=None):
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
                driver.get(year_url)
                
                try:
                    # [STEP 2] Decade(10년 단위) 탭 처리
                    decade_str = f"{str(year // 10 * 10)}s"
                    # 'year' 클래스가 없는 순수 탭 블록에서 Decade 버튼 탐색
                    decade_xpath = f"//div[contains(@class, 'issue-details-past-tabs') and not(contains(@class, 'year'))]//a[text()='{decade_str}']"
                    
                    try:
                        decade_button = wait.until(EC.presence_of_element_located((By.XPATH, decade_xpath)))
                        decade_li = decade_button.find_element(By.XPATH, "./parent::li")
                        
                        # Decade가 활성화되어 있지 않다면 클릭
                        if "active" not in decade_li.get_attribute("class"):
                            print(f"📂 {decade_str} 탭 활성화 시도...")
                            driver.execute_script("arguments[0].click();", decade_button)
                            # Decade가 active가 될 때까지 명시적 대기[cite: 3]
                            wait.until(lambda d: "active" in decade_li.get_attribute("class"))
                            time.sleep(2)
                    except:
                        pass # 이미 열려있거나 버튼이 없는 경우(TCAS 등)는 통과[cite: 3]

                    # [STEP 3] Year(세부 연도) 탭 처리[cite: 3]
                    # 'year' 클래스가 포함된 블록에서 해당 연도 버튼 탐색[cite: 3]
                    year_xpath = f"//div[contains(@class, 'year')]//a[text()='{year}']"
                    year_button = wait.until(EC.element_to_be_clickable((By.XPATH, year_xpath)))
                    year_li = year_button.find_element(By.XPATH, "./parent::li")

                    # 연도가 활성화되어 있지 않다면 클릭[cite: 3]
                    if "active" not in year_li.get_attribute("class"):
                        driver.execute_script("arguments[0].click();", year_button)
                        # 세부 연도가 active가 될 때까지 대기[cite: 3]
                        wait.until(lambda d: "active" in year_li.get_attribute("class"))
                        time.sleep(3)

                    # [STEP 4] 이슈 목록 로드 대기[cite: 3]
                    wait.until(EC.presence_of_element_located((By.PARTIAL_LINK_TEXT, "Issue ")))
                    issue_elements = driver.find_elements(By.PARTIAL_LINK_TEXT, "Issue ")
                    issue_info = [{"text": el.text, "url": el.get_attribute("href")} for el in issue_elements]
                    print(f"✅ {year}년 활성화 성공 (이슈 {len(issue_info)}개)")

                except Exception as e:
                    print(f"⚠️ {year}년 로딩 실패. 패스합니다.")
                    continue

                # [STEP 5] 이슈별 논문 수집 로직 (기존 유지)[cite: 3]
                for info in issue_info:
                    issue_text = info["text"]
                    paged_url = f"{info['url']}&rowsPerPage=100"
                    
                    for attempt in range(3):
                        try:
                            print(f"🔎 {journal['name']} {year} {issue_text} ({attempt+1}/3)...", end=" ", flush=True)
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
                            time.sleep(5)
                            continue

                    driver.get(year_url)
                
                # 연도별 중간 저장[cite: 3]
                if all_data:
                    pd.DataFrame(all_data).to_excel(output_path, index=False)

        print(f"\n✨ 수집 완료! 최종 파일: {output_path}")

    except Exception as e: print(f"🔥 오류: {e}")
    finally: driver.quit()

if __name__ == "__main__":
    # 2026년부터 2006년까지 전수 조사[cite: 3]
    sweep_ieee_target_range(2026, 2006)