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

def sweep_ieee_target_range(start_year=2026, end_year=2019, journals=None):
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
    wait = WebDriverWait(driver, 25) # 기본 대기 시간

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
                    # [명시적 대기 강화 구간]
                    year_xpath = f"//a[text()='{year}' and contains(@data-analytics_identifier, 'year')]"
                    
                    try:
                        # 1. 연도 버튼이 이미 보이는지 확인 (TCAS 등)
                        year_button = wait.until(EC.element_to_be_clickable((By.XPATH, year_xpath)))
                    except:
                        # 2. 안 보인다면 Decade 버튼(예: 2010s) 찾아 클릭 (JSSC 등)
                        decade_str = f"{str(year // 10 * 10)}s"
                        print(f"📂 {year}년이 숨겨져 있음. {decade_str} 탭 활성화 시도...")
                        decade_xpath = f"//a[text()='{decade_str}']"
                        decade_button = wait.until(EC.element_to_be_clickable((By.XPATH, decade_xpath)))
                        driver.execute_script("arguments[0].click();", decade_button)
                        
                        # [핵심] Decade 클릭 후 '연도 버튼'이 화면에 실제로 나타날 때까지 대기
                        year_button = wait.until(EC.visibility_of_element_located((By.XPATH, year_xpath)))
                        year_button = wait.until(EC.element_to_be_clickable((By.XPATH, year_xpath)))

                    # 3. 연도 탭 활성화 확인[cite: 3]
                    parent_li = year_button.find_element(By.XPATH, "./parent::li")
                    if "active" not in parent_li.get_attribute("class"):
                        driver.execute_script("arguments[0].click();", year_button)
                        # 리스트가 갱신되어 'active' 클래스가 붙을 때까지 대기
                        wait.until(lambda d: "active" in parent_li.get_attribute("class"))
                        time.sleep(2)

                    # 4. 이슈 목록 로드 대기[cite: 3]
                    wait.until(EC.presence_of_element_located((By.PARTIAL_LINK_TEXT, "Issue ")))
                    issue_elements = driver.find_elements(By.PARTIAL_LINK_TEXT, "Issue ")
                    issue_info = [{"text": el.text, "url": el.get_attribute("href")} for el in issue_elements]
                except Exception as e:
                    print(f"⚠️ {year}년 로딩 실패. (현재 페이지 상태 불안정) 패스합니다.")
                    continue

                for info in issue_info:
                    issue_text = info["text"]
                    paged_url = f"{info['url']}&rowsPerPage=100"
                    
                    for attempt in range(3): # 재시도 로직 유지[cite: 3]
                        try:
                            print(f"🔎 {journal['name']} {year} {issue_text} ({attempt+1}/3)...", end=" ", flush=True)
                            driver.get(paged_url)
                            # 논문 리스트 컨테이너가 뜰 때까지 명시적 대기
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
    sweep_ieee_target_range(2026, 2019)