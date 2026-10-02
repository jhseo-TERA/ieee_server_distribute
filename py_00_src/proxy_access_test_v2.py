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

def access_ieee_journals():
    # 경로 및 URL 설정
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    download_dir = os.path.join(base_dir, "py_01_data", "01_library")
    os.makedirs(download_dir, exist_ok=True)

    proxy_prefix = "https://access.yonsei.ac.kr/link.n2s?url="
    target_urls = [
        "https://ieeexplore.ieee.org/xpl/issues?punumber=4",    # JSSC
        "https://ieeexplore.ieee.org/xpl/issues?punumber=8919", # TCAS-I
        "https://ieeexplore.ieee.org/xpl/issues?punumber=8920"  # TCAS-II
    ]

    # Edge 옵션 설정
    options = Options()
    prefs = {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "plugins.always_open_pdf_externally": True
    }
    options.add_experimental_option("prefs", prefs)

    print("🚀 Selenium을 시작합니다...")
    driver = webdriver.Edge(options=options)
    wait = WebDriverWait(driver, 15) # 요소가 나타날 때까지 최대 15초 대기

    try:
        # --- [STEP 1] 첫 번째 저널 접속 및 자동 로그인 ---
        first_url = proxy_prefix + target_urls[0]
        print(f"🔗 [1/3] 접속 및 로그인 시도: {first_url}")
        driver.get(first_url)

        # 아이디 입력 (id="id")
        id_field = wait.until(EC.presence_of_element_located((By.ID, "id")))
        id_field.send_keys(os.getenv("YONSEI_ID"))

        # 비밀번호 입력 (id="password")
        pw_field = driver.find_element(By.ID, "password")
        pw_field.send_keys(os.getenv("YONSEI_PW"))

        # 로그인 버튼 클릭 (input[type='submit'][value='로그인'])
        login_btn = driver.find_element(By.XPATH, "//input[@type='submit' and @value='로그인']")
        login_btn.click()
        print("✅ 로그인 정보 전송 완료")

        # --- [STEP 2] 나머지 저널 새 탭으로 열기 ---
        # 로그인이 처리되는 시간을 고려하여 잠시 대기
        time.sleep(5) 

        for idx, base_url in enumerate(target_urls[1:], start=2):
            full_url = proxy_prefix + base_url
            print(f"🔗 [{idx}/3] 새 탭 열기: {full_url}")
            driver.execute_script(f"window.open('{full_url}');")
            time.sleep(2)

        print(f"✨ 모든 저널 탭 이동 완료!")
        
        # 확인을 위해 30초간 브라우저 유지 (이후 필요에 따라 수정)
        input("🛋️ 브라우저 확인이 끝나면 엔터를 눌러 종료하세요...")

    except Exception as e:
        print(f"❌ 오류 발생: {e}")
    finally:
        if 'driver' in locals():
            driver.quit()
        print("🔒 작업을 종료합니다.")

if __name__ == "__main__":
    access_ieee_journals()