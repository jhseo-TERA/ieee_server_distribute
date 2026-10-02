import os
import time
import pandas as pd
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# .env 파일 로드 (아이디/비번 보안)
load_dotenv()

def sweep_ieee_target_range(start_year=2026, end_year=2020):
    # 1. 경로 및 파일 설정
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
    wait = WebDriverWait(driver, 30) # 대기 시간 30초 설정

    try:
        # [STEP 1] 로그인 세션 확보
        print("🔐 연세대 프록시 로그인 시도 중...")
        driver.get(f"{proxy_prefix}https://ieeexplore.ieee.org/xpl/issues?punumber=4")
        wait.until(EC.presence_of_element_located((By.ID, "id"))).send_keys(os.getenv("YONSEI_ID"))
        driver.find_element(By.ID, "password").send_keys(os.getenv("YONSEI_PW"))
        driver.find_element(By.XPATH, "//input[@type='submit' and @value='로그인']").click()
        time.sleep(5)

        # [STEP 2] 저널 -> 연도 -> 이슈 순회
        for journal in journals:
            print(f"\n📘 [{journal['name']}] 스캔 시작")
            
            for year in range(start_year, end_year - 1, -1):
                year_url = f"{proxy_prefix}https://ieeexplore.ieee.org/xpl/issues?punumber={journal['id']}&isyear={year}"
                print(f"\n📅 {year}년 페이지 접속 시도...")
                driver.get(year_url)
                
                try:
                    # [핵심 수정] 해당 연도 탭이 실제로 활성화('active') 될 때까지 대기
                    # 이를 통해 이전 연도 페이지에서 최신 연도(2026) 리스트가 중복 수집되는 것을 방지
                    year_tab_xpath = f"//a[text()='{year}']/parent::li[contains(@class, 'active')]"
                    wait.until(EC.presence_of_element_located((By.XPATH, year_tab_xpath)))
                    
                    # 리스트가 완전히 갈아끼워지도록 안정화 대기
                    time.sleep(2)
                    
                    # 이슈 목록 가져오기
                    issue_elements = driver.find_elements(By.PARTIAL_LINK_TEXT, "Issue ")
                    issue_info = [{"text": el.text, "url": el.get_attribute("href")} for el in issue_elements]
                    
                    if not issue_info:
                        print(f"⚠️ {year}년 이슈 목록이 없습니다. 패스합니다.")
                        continue
                        
                except Exception as e:
                    print(f"⚠️ {year}년 이슈 목록 로딩 실패 또는 연도 탭 활성화 지연. 패스합니다.")
                    continue

                for info in issue_info:
                    issue_text = info["text"]
                    # 100개씩 보기 파라미터 강제 주입
                    paged_url = f"{info['url']}&rowsPerPage=100"
                    
                    # --- [RETRY LOGIC] ---
                    max_retries = 3
                    success = False
                    
                    for attempt in range(max_retries):
                        try:
                            print(f"🔎 {journal['name']} {year} {issue_text} 진입 시도 ({attempt+1}/{max_retries})...", end=" ", flush=True)
                            driver.get(paged_url)
                            
                            # 데이터 로딩 대기 (컨테이너 및 저자 정보 확인)
                            wait.until(EC.presence_of_element_located((By.CLASS_NAME, "List-results-items")))
                            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "p.author")))
                            time.sleep(3) 

                            # 개별 논문 블록 추출[cite: 1]
                            paper_items = driver.find_elements(By.CLASS_NAME, "List-results-items")
                            
                            count = 0
                            for item in paper_items:
                                try:
                                    # 제목 및 URL 추출 (xplmathjax 속성 활용)[cite: 1]
                                    title_el = item.find_element(By.CSS_SELECTOR, "a[xplmathjax]")
                                    title_text = title_el.text.strip()
                                    link_url = title_el.get_attribute("href")
                                    
                                    # 저자 정보 추출[cite: 1]
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
                                except:
                                    continue
                            
                            print(f"✅ {count}개 수집 완료")
                            success = True
                            break 
                            
                        except Exception as e:
                            if attempt < max_retries - 1:
                                print(f"⚠️ 실패, 5초 후 새로고침 및 재시도...")
                                time.sleep(5)
                                continue 
                            else:
                                print(f"❌ {max_retries}회 시도 모두 실패. 다음 이슈로 넘어갑니다.")
                    # --- [RETRY LOGIC END] ---

                    # 연도 페이지로 원상복구
                    driver.get(year_url)
                    time.sleep(1.5)
                
                # 연도별 중간 저장 (데이터 유실 방지)
                if all_data:
                    pd.DataFrame(all_data).to_excel(output_path, index=False)

        print(f"\n✨ 전수 조사 성공! 최종 파일: {output_path}")

    except Exception as e:
        print(f"🔥 치명적 오류: {e}")
    finally:
        driver.quit()

if __name__ == "__main__":
    # 2026년부터 2020년까지 7년치 전수 조사 실행
    sweep_ieee_target_range(2026, 2020)