import os
import time
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# 1. 환경 변수 로드 (.env 파일에 YONSEI_ID, YONSEI_PW가 있어야 함)
load_dotenv()

def access_ieee_journals_by_issue(target_year, target_issue):
    # 경로 및 저널 ID 설정
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    download_dir = os.path.join(base_dir, "py_01_data", "01_library")
    os.makedirs(download_dir, exist_ok=True)

    proxy_prefix = "https://access.yonsei.ac.kr/link.n2s?url="
    # 4: JSSC, 8919: TCAS-I, 8920: TCAS-II
    journals = [
        {"name": "JSSC", "id": "4"},
        {"name": "TCAS-I", "id": "8919"},
        {"name": "TCAS-II", "id": "8920"}
    ]

    # Edge 옵션 설정
    options = Options()
    prefs = {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "plugins.always_open_pdf_externally": True
    }
    options.add_experimental_option("prefs", prefs)

    print(f"🚀 Selenium 시작: {target_year}년 Issue {target_issue} 탐색")
    driver = webdriver.Edge(options=options)
    wait = WebDriverWait(driver, 15)

    try:
        # --- [STEP 1] 첫 번째 저널 접속 및 자동 로그인 ---
        first_journal = journals[0]
        # All Issues 페이지에 연도 파라미터를 붙여서 바로 이동
        first_url = f"{proxy_prefix}https://ieeexplore.ieee.org/xpl/issues?punumber={first_journal['id']}&isyear={target_year}"
        
        print(f"🔗 [1/3] {first_journal['name']} 접속 및 로그인 시도...")
        driver.get(first_url)

        # 로그인 정보 입력
        wait.until(EC.presence_of_element_located((By.ID, "id"))).send_keys(os.getenv("YONSEI_ID"))
        driver.find_element(By.ID, "password").send_keys(os.getenv("YONSEI_PW"))
        driver.find_element(By.XPATH, "//input[@type='submit' and @value='로그인']").click()
        print("✅ 로그인 완료")

        # 로그인 처리 후 이슈 클릭 함수 실행 (첫 번째 탭)
        select_issue(driver, wait, target_issue)

        # --- [STEP 2] 나머지 저널 새 탭으로 열기 및 이슈 이동 ---
        for idx, journal in enumerate(journals[1:], start=2):
            full_url = f"{proxy_prefix}https://ieeexplore.ieee.org/xpl/issues?punumber={journal['id']}&isyear={target_year}"
            print(f"🔗 [{idx}/3] {journal['name']} 새 탭 열기...")
            
            driver.execute_script(f"window.open('{full_url}');")
            time.sleep(2)
            
            # 새 탭으로 제어권 이동
            driver.switch_to.window(driver.window_handles[-1])
            
            # 해당 탭에서 이슈 클릭
            select_issue(driver, wait, target_issue)

        print(f"✨ 모든 저널 {target_year}년 Issue {target_issue} 진입 완료!")
        
        input("🛋️ 논문 목록을 확인하신 후 엔터를 눌러 종료하세요...")

    except Exception as e:
        print(f"❌ 오류 발생: {e}")
    finally:
        if 'driver' in locals():
            driver.quit()
        print("🔒 작업을 종료합니다.")

def select_issue(driver, wait, issue_num):
    """현재 활성화된 탭에서 특정 이슈 번호를 찾아 클릭합니다."""
    try:
        issue_text = f"Issue {issue_num}"
        print(f"🔎 {issue_text} 링크를 찾는 중...")
        
        # 'Issue X' 텍스트를 포함한 클릭 가능한 요소를 찾음
        issue_link = wait.until(EC.element_to_be_clickable((By.PARTIAL_LINK_TEXT, issue_text)))
        issue_link.click()
        
        # 페이지 로딩 대기
        time.sleep(3)
        print(f"✅ {issue_text} 진입 성공")
    except Exception as e:
        print(f"⚠️ {issue_text}를 찾을 수 없습니다. (발행되지 않았거나 로딩 문제)")

if __name__ == "__main__":
    # 원하는 연도와 이슈 번호를 설정하세요
    access_ieee_journals_by_issue(target_year=2024, target_issue=5)