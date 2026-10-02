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

def sweep_ieee_target_range(start_year=2026, end_year=2020):
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
    wait = WebDriverWait(driver, 30)

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
                    # 연도별 이슈 목록 로딩 대기
                    wait.until(EC.presence_of_element_located((By.PARTIAL_LINK_TEXT, "Issue ")))
                    time.sleep(2)
                    
                    issue_elements = driver.find_elements(By.PARTIAL_LINK_TEXT, "Issue ")
                    # 이슈들의 텍스트와 원본 URL(href)을 미리 저장합니다.
                    issue_info = [{"text": el.text, "url": el.get_attribute("href")} for el in issue_elements]
                except:
                    print(f"⚠️ {year}년 이슈 목록 로딩 실패. 패스합니다.")
                    continue

                for info in issue_info:
                    try:
                        issue_text = info["text"]
                        # 핵심 수정: URL 뒤에 100개 보기 파라미터 추가
                        paged_url = f"{info['url']}&rowsPerPage=100"
                        
                        print(f"🔎 {journal['name']} {year} {issue_text} 진입 중 (100개 모드)...", end=" ", flush=True)
                        
                        # 클릭 대신 수정된 URL로 직접 이동
                        driver.get(paged_url)
                        
                        # 제목 렌더링 대기
                        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "a[xplmathjax]")))
                        time.sleep(3) 

                        paper_links = driver.find_elements(By.CSS_SELECTOR, "a[xplmathjax]")

                        count = 0
                        for link in paper_links:
                            title_text = link.text.strip()
                            link_url = link.get_attribute("href")
                            
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
                        
                        # 다음 이슈를 위해 연도 페이지로 복귀
                        driver.get(year_url)
                        wait.until(EC.presence_of_element_located((By.PARTIAL_LINK_TEXT, "Issue ")))

                    except Exception as e:
                        print(f"❌ 이슈 처리 실패")
                        driver.get(year_url)
                        time.sleep(2)
                
                if all_data:
                    pd.DataFrame(all_data).to_excel(output_path, index=False)

        print(f"\n✨ 전수 조사 성공! 파일: {output_path}")

    except Exception as e:
        print(f"🔥 치명적 오류: {e}")
    finally:
        driver.quit()

if __name__ == "__main__":
    sweep_ieee_target_range(2026, 2020)