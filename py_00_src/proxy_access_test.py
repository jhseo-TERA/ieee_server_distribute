import time
import os
import glob
from selenium import webdriver
from selenium.webdriver.edge.service import Service
from selenium.webdriver.edge.options import Options

def find_local_driver():
    """내 컴퓨터의 .wdm 폴더에서 이미 다운로드된 msedgedriver.exe를 찾습니다."""
    user_home = os.path.expanduser("~")
    # .wdm 폴더 내의 msedgedriver.exe 경로 검색
    search_path = os.path.join(user_home, ".wdm", "**", "msedgedriver.exe")
    files = glob.glob(search_path, recursive=True)
    
    if files:
        # 가장 최근에 수정된 드라이버 반환
        return max(files, key=os.path.getmtime)
    return None

def access_ieee_via_yonsei():
    proxy_prefix = "https://access.yonsei.ac.kr/link.n2s?url="
    target_urls = [
        "https://ieeexplore.ieee.org/xpl/RecentIssue.jsp?punumber=4",    # JSSC
        "https://ieeexplore.ieee.org/xpl/RecentIssue.jsp?punumber=8919", # CICC
        "https://ieeexplore.ieee.org/xpl/RecentIssue.jsp?punumber=8920"  # VLSI
    ]

    options = Options()
    
    # 1. 로컬에서 드라이버 찾기
    driver_path = find_local_driver()
    
    if not driver_path:
        print("❌ 로컬 드라이버를 찾을 수 없습니다. 인터넷 연결을 확인하고 다시 시도하거나 드라이버를 수동으로 배치해야 합니다.")
        return

    print(f"✅ 로컬 드라이버 사용: {driver_path}")
    
    # 2. 드라이버 실행
    try:
        service = Service(executable_path=driver_path)
        driver = webdriver.Edge(service=service, options=options)
        
        for idx, base_url in enumerate(target_urls):
            full_url = proxy_prefix + base_url
            print(f"🚀 [{idx+1}/{len(target_urls)}] 접속 시도: {full_url}")
            
            driver.get(full_url)

            if idx == 0:
                print("💡 첫 접속입니다. 연세대 로그인을 진행해 주세요.")
                input("🔑 로그인을 완료한 후 엔터(Enter)를 입력하면 다음 저널로 이동합니다...")
            else:
                time.sleep(3)

        print("✨ 모든 저널 접속 성공!")
        time.sleep(5)

    except Exception as e:
        print(f"🔥 오류 발생: {e}")
    finally:
        if 'driver' in locals():
            driver.quit()

if __name__ == "__main__":
    access_ieee_via_yonsei()