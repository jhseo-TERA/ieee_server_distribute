import os
import requests
import json
from dotenv import load_dotenv

# 1. 환경 변수 및 경로 로드
# 현재 스크립트 위치(py_00_src) 기준 상위 폴더의 .env 파일을 찾습니다.
base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(base_dir, ".env"))

def test_ieee_api():
    api_key = os.getenv("IEEE_API_KEY")
    if not api_key:
        print("❌ 에러: .env 파일에서 API 키를 찾을 수 없습니다.")
        return

    # 저장 경로 설정
    save_dir = os.path.join(base_dir, "py_01_data", "00_metadata")
    os.makedirs(save_dir, exist_ok=True)

    # 2. API 호출 설정 (Wireline SerDes 검색)
    url = "https://ieeexploreapi.ieee.org/api/v1/search/articles"
    params = {
        'apikey': api_key,
        'format': 'json',
        'article_title': 'Wireline SerDes',
        'max_records': 100
    }
    
    print("🔍 IEEE API 호출 중...")
    response = requests.get(url, params=params)
    
    if response.status_code == 200:
        data = response.json()
        if 'articles' in data:
            # 3. 파일 저장
            file_path = os.path.join(save_dir, "test_search_result.json")
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            
            paper = data['articles'][0]
            print(f"✅ 수집 및 저장 완료: {file_path}")
            print(f"📝 논문 제목: {paper['title']}")
        else:
            print("⚠️ 검색 결과가 없습니다.")
    else:
        print(f"❌ 에러 발생 (코드 {response.status_code}): {response.text}")

if __name__ == "__main__":
    test_ieee_api()