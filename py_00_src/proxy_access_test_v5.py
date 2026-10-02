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
    # 1. 경로 설정
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
    # 브라우저 창을 띄운 상태로 진행 (사람처럼 보이게 하기 위함)
    driver = webdriver.Edge(options=options)
    wait = WebDriverWait(driver, 20) # 대기 시간을 20초로 넉넉히 설정

    try:
        # [STEP 1] 로그인 세션 확보
        print("🔐 연세대 프록시 로그인을 시작합니다...")
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
                driver.get(year_url)
                time.sleep(3) # 연도 페이지 로딩 대기

                # 현재 연도에 있는 Issue 링크들 수집
                issue_elements = driver.find_elements(By.PARTIAL_LINK_TEXT, "Issue ")
                issue_texts = [el.text for el in issue_elements]

                if not issue_texts:
                    print(f"⚠️ {year}년에 발행된 이슈가 아직 없습니다. 패스합니다.")
                    continue

                for issue_text in issue_texts:
                    try:
                        print(f"🔎 {journal['name']} {year} {issue_text} 진입 중...", end=" ", flush=True)
                        
                        # 이슈 링크 클릭
                        issue_link = wait.until(EC.element_to_be_clickable((By.LINK_TEXT, issue_text)))
                        issue_link.click()
                        
                        # [핵심] 제목이 나타날 때까지 명시적 대기
                        # '.result-item-title' 클래스가 나타나야 제목을 긁을 수 있음
                        wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, ".result-item-title a")))
                        time.sleep(2) # 안정성을 위한 추가 대기

                        titles = driver.find_elements(By.CSS_SELECTOR, ".result-item-title a")
                        
                        count = 0
                        for t in titles:
                            title_text = t.text.strip()
                            if title_text: # 빈 제목 제외
                                all_data.append({
                                    "Journal": journal['name'],
                                    "Year": year,
                                    "Issue": issue_text,
                                    "Title": title_text,
                                    "URL": t.get_attribute("href")
                                })
                                count += 1
                        
                        print(f"✅ {count}개 수집 완료")
                        
                        # 다시 연도 페이지로 복귀
                        driver.back()
                        time.sleep(1.5)

                    except Exception as e:
                        print(f"❌ 실패 (재시도 중...): {e}")
                        driver.get(year_url) # 에러 시 연도 페이지 재접속
                        time.sleep(2)
                
                # 연도별 저장 (데이터 유실 방지)
                if all_data:
                    df = pd.DataFrame(all_data)
                    df.to_excel(output_path, index=False)
                    print(f"💾 {year}년까지 중간 저장 완료. (누적 {len(all_data)}건)")

        print(f"\n✨ 전수 조사 완료! 최종 파일: {output_path}")

    except Exception as e:
        print(f"🔥 치명적 오류: {e}")
    finally:
        driver.quit()

if __name__ == "__main__":
    sweep_ieee_target_range(start_year=2026, end_year=2020)