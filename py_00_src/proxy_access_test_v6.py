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

def sweep_ieee_target_range(start_year=2026, end_year=2024):
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
    wait = WebDriverWait(driver, 30) # 대기 시간을 30초로 늘림

    try:
        # [STEP 1] 로그인
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
                    # 버그 1 해결: 이슈 링크("Issue ")가 하나라도 나타날 때까지 대기
                    wait.until(EC.presence_of_element_located((By.PARTIAL_LINK_TEXT, "Issue ")))
                    time.sleep(2) # 안정화 대기
                    
                    issue_elements = driver.find_elements(By.PARTIAL_LINK_TEXT, "Issue ")
                    issue_texts = [el.text for el in issue_elements]
                except:
                    print(f"⚠️ {year}년 이슈 목록 로딩 실패. 패스합니다.")
                    continue

                for issue_text in issue_texts:
                    try:
                        print(f"🔎 {journal['name']} {year} {issue_text} 진입 중...", end=" ", flush=True)
                        
                        # 이슈 클릭
                        issue_link = wait.until(EC.element_to_be_clickable((By.PARTIAL_LINK_TEXT, issue_text)))
                        driver.execute_script("arguments[0].click();", issue_link) # 안정적인 클릭을 위해 스크립트 사용
                        
                        # 1. 제목이 담긴 링크가 나타날 때까지 대기 (xplmathjax 속성 활용)
                        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "a[xplmathjax]")))
                        time.sleep(2) # 안정적인 렌더링을 위해 살짝 더 대기

                        # 2. xplmathjax 속성을 가진 모든 <a> 태그를 찾아 제목과 URL 추출
                        paper_links = driver.find_elements(By.CSS_SELECTOR, "a[xplmathjax]")

                        count = 0
                        for link in paper_links:
                            title_text = link.text.strip()
                            link_url = link.get_attribute("href")
                            
                            # 제목이 비어있지 않고, 실제 논문 링크(/document/)인 경우만 저장
                            if title_text and "/document/" in link_url:
                                all_data.append({
                                    "Journal": journal['name'],
                                    "Year": year,
                                    "Issue": issue_text,
                                    "Title": title_text,
                                    "URL": link_url
                                })
                                count += 1

                        print(f"✅ {count}개 수집 완료")
                        driver.back()
                        wait.until(EC.presence_of_element_located((By.PARTIAL_LINK_TEXT, "Issue "))) # 돌아왔을 때 재로딩 대기

                    except Exception as e:
                        print(f"❌ 이슈 진입 실패")
                        driver.get(year_url) # 에러 시 원상복구
                        time.sleep(2)
                
                # 연도별 중간 저장
                if all_data:
                    pd.DataFrame(all_data).to_excel(output_path, index=False)

        print(f"\n✨ 전수 조사 성공! 파일: {output_path}")

    except Exception as e:
        print(f"🔥 치명적 오류: {e}")
    finally:
        driver.quit()

if __name__ == "__main__":
    sweep_ieee_target_range(2026, 2020)