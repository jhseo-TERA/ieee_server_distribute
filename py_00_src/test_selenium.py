import os
import time
from selenium import webdriver
from selenium.webdriver.edge.service import Service
from selenium.webdriver.edge.options import Options

def test_edge_browser():
    # 1. 경로 설정 (동일하게 유지)
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    download_dir = os.path.join(base_dir, "py_01_data", "01_library")
    os.makedirs(download_dir, exist_ok=True)

    # 2. Edge 옵션 설정
    options = Options()
    
    # 다운로드 설정
    prefs = {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "plugins.always_open_pdf_externally": True
    }
    options.add_experimental_option("prefs", prefs)
    
    # 💥 핵심 변경: 별도의 Manager 라이브러리 없이 실행
    print("🚀 Selenium 내장 매니저를 사용하여 Edge를 실행합니다...")
    
    try:
        # Service() 안에 경로를 비워두면 Selenium이 시스템에서 Edge를 자동으로 찾습니다.
        driver = webdriver.Edge(options=options)
        
        driver.get("https://www.bing.com")
        print(f"✅ 접속 성공: {driver.title}")
        
        time.sleep(5)
        
    except Exception as e:
        print(f"❌ 실행 실패: {e}")
        print("\n💡 만약 계속 오프라인 에러가 난다면, 연구소 내부 보안 정책으로 인해")
        print("   드라이버 자동 다운로드가 막힌 것일 수 있습니다.")
        
    finally:
        if 'driver' in locals():
            driver.quit()
        print("🔒 작업을 종료합니다.")

if __name__ == "__main__":
    test_edge_browser()