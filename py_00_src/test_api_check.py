import requests
import os
from dotenv import load_dotenv

# .env 파일에서 키를 가져오거나 직접 입력하세요
# 현재는 테스트를 위해 직접 입력을 권장합니다.
base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(base_dir, ".env"))
API_KEY = os.getenv("IEEE_API_KEY", "").strip()

def check_api_status():
    if not API_KEY:
        raise RuntimeError("IEEE_API_KEY is not configured in .env")
    print(f"🚀 IEEE API 연결 테스트 시작...")
    
    # 가장 가벼운 검색어 'circuit'으로 1건만 요청
    url = f"https://ieeexploreapi.ieee.org/api/v1/search/articles?apikey={API_KEY}&format=json&max_records=1&article_title=circuit"
    
    try:
        response = requests.get(url)
        print(f"📊 Status Code: {response.status_code}")
        
        if response.status_code == 200:
            print("✅ [성공] API 키가 활성화되었습니다! 이제 개발을 시작하셔도 됩니다.")
        elif response.status_code == 403:
            print("❌ [대기] 403 Forbidden: 아직 승인 대기 중이거나 권한이 활성화되지 않았습니다.")
        elif response.status_code == 401:
            print("❌ [오류] 401 Unauthorized: API 키가 잘못되었거나 유효하지 않습니다.")
        else:
            print(f"⚠️ [기타] 예상치 못한 응답입니다: {response.text}")
            
    except Exception as e:
        print(f"🔥 [에러] 네트워크 연결에 문제가 있습니다: {e}")

if __name__ == "__main__":
    check_api_status()
