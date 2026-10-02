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

def sweep_ieee_target_range(start_year=2026, end_year=2025):
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    output_path = os.path.join(base_dir, "py_01_data", "00_metadata", f"IEEE_Sweep_{start_year}_{end_year}.xlsx")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    proxy_prefix = "https://access.yonsei.ac.kr/link.n2s?url="
    journals = [
        {"name": "JSSC", "id": "4"},
        {"name": "TCAS-I", "id": "8919"},
        {"name": "TCAS-II", "id": "8920"}
    ]

    all_data = []
    options = Options()
    driver = webdriver.Edge(options=options)
    wait = WebDriverWait(driver, 25) # 대기 시간 최적화

    try:
        # [STEP 1] 로그인
        print("🔐 연세대 프록시 로그인 시도...")
        driver.get(f"{proxy_prefix}https://ieeexplore.ieee.org/xpl/issues?punumber=4")
        wait.until(EC.presence_of_element_located((By.ID, "id"))).send_keys(os.getenv("YONSEI_ID"))
        driver.find_element(By.ID, "password").send_keys(os.getenv("YONSEI_PW"))
        driver.find_element(By.XPATH, "//input[@type='submit' and @value='로그인']").click()
        time.sleep(5)

        for journal in journals:
            print(f"\n📘 [{journal['name']}] 스캔 시작")
            
            for year in range(start_year, end_year - 1, -1):
                year_url = f"{proxy_prefix}https://ieeexplore.ieee.org/xpl/issues?punumber={journal['id']}&isyear={year}"
                print(f"\n📅 {year}년 페이지 접속 시도...")
                driver.get(year_url)
                
                try:
                    # [핵심 수정] 연도 탭 강제 클릭 및 활성화 확인 로직
                    # 1. 해당 연도 텍스트를 가진 <a> 태그 찾기
                    year_xpath = f"//a[text()='{year}' and contains(@data-analytics_identifier, 'year')]"
                    year_button = wait.until(EC.presence_of_element_located((By.XPATH, year_xpath)))
                    
                    # 2. 부모 <li>의 클래스 확인
                    parent_li = year_button.find_element(By.XPATH, "./parent::li")
                    
                    if "active" not in parent_li.get_attribute("class"):
                        print(f"🖱️ {year}년 탭이 비활성 상태입니다. 클릭하여 전환합니다.")
                        driver.execute_script("arguments[0].click();", year_button)
                        
                        # 3. 클릭 후 부모 <li>에 'active' 클래스가 생길 때까지 대기
                        wait.until(lambda d: "active" in parent_li.get_attribute("class"))
                        time.sleep(3) # 리스트가 완전히 교체될 시간 확보
                    
                    print(f"✅ {year}년 탭 활성화 완료. 이슈 목록을 수집합니다.")

                    # 이슈 목록 가져오기
                    issue_elements = driver.find_elements(By.PARTIAL_LINK_TEXT, "Issue ")
                    issue_info = [{"text": el.text, "url": el.get_attribute("href")} for el in issue_elements]
                    
                    if not issue_info:
                        print(f"⚠️ {year}년 이슈 목록을 찾을 수 없습니다.")
                        continue
                        
                except Exception as e:
                    print(f"⚠️ {year}년 탭 전환 실패. 다음 연도로 넘어갑니다.")
                    continue

                for info in issue_info:
                    issue_text = info["text"]
                    paged_url = f"{info['url']}&rowsPerPage=100" # 100개 보기 파라미터
                    
                    max_retries = 3
                    for attempt in range(max_retries):
                        try:
                            print(f"🔎 {journal['name']} {year} {issue_text} 진입 시도 ({attempt+1}/{max_retries})...", end=" ", flush=True)
                            driver.get(paged_url)
                            
                            # 제목과 저자가 뜰 때까지 대기
                            wait.until(EC.presence_of_element_located((By.CLASS_NAME, "List-results-items")))
                            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "p.author")))
                            time.sleep(2) 

                            paper_items = driver.find_elements(By.CLASS_NAME, "List-results-items")
                            
                            count = 0
                            for item in paper_items:
                                try:
                                    title_el = item.find_element(By.CSS_SELECTOR, "a[xplmathjax]")
                                    title_text = title_el.text.strip()
                                    link_url = title_el.get_attribute("href")
                                    
                                    try:
                                        author_el = item.find_element(By.CSS_SELECTOR, "p.author")
                                        author_text = author_el.text.strip()
                                    except:
                                        author_text = "N/A"

                                    if title_text and "/document/" in link_url:
                                        all_data.append({
                                            "Journal": journal['name'],
                                            "Year": year,
                                            "Issue": issue_text,
                                            "Title": title_text,
                                            "Authors": author_text,
                                            "URL": link_url
                                        })
                                        count += 1
                                except: continue
                            
                            print(f"✅ {count}개 수집 완료")
                            break 
                            
                        except Exception as e:
                            if attempt < max_retries - 1:
                                time.sleep(5)
                                continue 
                            else: print(f"❌ 실패")

                    driver.get(year_url)
                    time.sleep(1)
                
                if all_data:
                    pd.DataFrame(all_data).to_excel(output_path, index=False)

        print(f"\n✨ 수집 완료! 파일: {output_path}")

    except Exception as e:
        print(f"🔥 치명적 오류: {e}")
    finally:
        driver.quit()

if __name__ == "__main__":
    sweep_ieee_target_range(2026, 2020)