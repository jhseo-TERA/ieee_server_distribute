import os
import time
import re
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

load_dotenv()

def clean_text(text):
    if not text: return ""
    text = text.replace('\n', ' ').replace('\r', ' ')
    return ' '.join(text.split())

def sweep_ieee_conferences(start_year=2026, end_year=2006, conferences=None):
    if conferences is None:
        conferences = [
            {"name": "ISSCC", "id": "1000708"},
            {"name": "VLSI-Circuits", "id": "1000798"},
            {"name": "VLSI-Tech", "id": "1000802"},
            {"name": "CICC", "id": "1000173"},
            {"name": "RFIC", "id": "1000611"},
            {"name": "ESSCIRC", "id": "1000709"},
            {"name": "ISCAS", "id": "1000089"},
            {"name": "ASSCC", "id": "1001114"},
            {"name": "MWSCAS", "id": "1000090"},
            {"name": "APCCAS", "id": "1000091"},
            {"name": "NEWCAS", "id": "1001283"}
        ]
    
    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = f"IEEE_Conf_Sweep_{start_year}_{end_year}_{now_str}.xlsx"
    
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    output_path = os.path.join(base_dir, "py_01_data", "00_metadata", file_name)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    proxy_prefix = "https://access.yonsei.ac.kr/link.n2s?url="
    all_data = []
    options = Options()
    driver = webdriver.Edge(options=options)
    wait = WebDriverWait(driver, 25)

    current_conf = "N/A"
    current_y = "N/A"
    current_step = "초기화"

    try:
        # [STEP 1] 로그인
        print(f"🔐 학회 수집 로그인 중...")
        driver.get(f"{proxy_prefix}https://ieeexplore.ieee.org/xpl/conhome/1000708/all-proceedings")
        wait.until(EC.presence_of_element_located((By.ID, "id"))).send_keys(os.getenv("YONSEI_ID"))
        driver.find_element(By.ID, "password").send_keys(os.getenv("YONSEI_PW"))
        driver.find_element(By.XPATH, "//input[@type='submit' and @value='로그인']").click()
        time.sleep(5)

        for conf in conferences:
            current_conf = conf['name']
            current_step = "All Proceedings 목록 분석"
            print(f"\n🚀 [{current_conf}] 목록 스캔 중...")
            
            # All Proceedings 페이지 접속
            all_proc_url = f"{proxy_prefix}https://ieeexplore.ieee.org/xpl/conhome/{conf['id']}/all-proceedings"
            driver.get(all_proc_url)
            
            try:
                # <li> 태그 내부의 학회 링크들 추출 (이미지 속의 그 목록)
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "li h2 a")))
                proc_links = driver.find_elements(By.CSS_SELECTOR, "li h2 a")
                
                target_proceedings = []
                for link in proc_links:
                    txt = link.text
                    url = link.get_attribute("href")
                    # 텍스트에서 연도 추출 (예: 2025 IEEE CICC -> 2025)
                    match = re.search(r'20\d{2}', txt)
                    if match:
                        y = int(match.group())
                        if end_year <= y <= start_year:
                            target_proceedings.append({"year": y, "url": url})
                
                print(f"✅ 대상 연도 {len(target_proceedings)}개 발견.")

                # [STEP 2] 개별 학회(연도별) 진입
                for proc in target_proceedings:
                    current_y = proc['year']
                    current_step = f"{current_y}년 논문 리스트 수집"
                    
                    # 수집 시 rowsPerPage=100 파라미터 추가
                    target_url = f"{proc['url']}"
                    if "?" in target_url: target_url += "&rowsPerPage=100"
                    else: target_url += "?rowsPerPage=100"

                    year_success = False
                    for attempt in range(1, 4):
                        try:
                            driver.get(target_url)
                            if attempt > 1: driver.refresh()
                            
                            wait.until(EC.presence_of_element_located((By.CLASS_NAME, "List-results-items")))
                            time.sleep(2)

                            paper_items = driver.find_elements(By.CLASS_NAME, "List-results-items")
                            count = 0
                            for item in paper_items:
                                try:
                                    title_el = item.find_element(By.CSS_SELECTOR, "a[xplmathjax]")
                                    title_text = clean_text(title_el.text)
                                    link_url = title_el.get_attribute("href")
                                    try:
                                        author_text = clean_text(item.find_element(By.CSS_SELECTOR, "p.author").text)
                                    except: author_text = "N/A"

                                    if title_text and "/document/" in link_url:
                                        all_data.append({
                                            "Conference": current_conf, "Year": current_y,
                                            "Title": title_text, "Authors": author_text, "URL": link_url
                                        })
                                        count += 1
                                except: continue
                            
                            print(f"🔎 {current_conf} {current_y}: ✅ {count}개 완료")
                            year_success = True
                            break
                        except:
                            time.sleep(10)

                    # 연도별 중간 저장
                    if all_data:
                        pd.DataFrame(all_data).to_excel(output_path, index=False)

            except Exception as e:
                print(f"⚠️ [{current_conf}] 목록을 불러오지 못했습니다. (Skip)")
                continue

        print(f"\n✨ 학회 수집 완주! 파일: {output_path}")

    except Exception as e:
        print("\n" + "="*50)
        print(f"🔥 치명적 오류 발생")
        print(f"📍 위치: {current_conf} / {current_y}")
        print(f"📍 단계: {current_step}")
        print(f"❌ 내용: {e}")
        print("="*50)
    finally:
        driver.quit()

if __name__ == "__main__":
    sweep_ieee_conferences(2026, 2006)