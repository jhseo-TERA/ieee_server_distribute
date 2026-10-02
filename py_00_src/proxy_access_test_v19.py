import os
import time
import re
import math
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
    current_page = 1

    try:
        # [STEP 1] 로그인
        print(f"🔐 학회 수집 로그인 및 세션 확보 중...")
        driver.get(f"{proxy_prefix}https://ieeexplore.ieee.org/xpl/conhome/1000708/all-proceedings")
        wait.until(EC.presence_of_element_located((By.ID, "id"))).send_keys(os.getenv("YONSEI_ID"))
        driver.find_element(By.ID, "password").send_keys(os.getenv("YONSEI_PW"))
        driver.find_element(By.XPATH, "//input[@type='submit' and @value='로그인']").click()
        time.sleep(5)

        for conf in conferences:
            current_conf = conf['name']
            print(f"\n🚀 [{current_conf}] All Proceedings 목록 분석 시작")
            all_proc_url = f"{proxy_prefix}https://ieeexplore.ieee.org/xpl/conhome/{conf['id']}/all-proceedings"
            driver.get(all_proc_url)
            
            try:
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "li h2 a")))
                proc_links = driver.find_elements(By.CSS_SELECTOR, "li h2 a")
                target_proceedings = []
                for link in proc_links:
                    txt, url = link.text, link.get_attribute("href")
                    match = re.search(r'20\d{2}', txt)
                    if match:
                        y = int(match.group())
                        if end_year <= y <= start_year:
                            target_proceedings.append({"year": y, "url": url})
                
                print(f"✅ {start_year}~{end_year} 구간 Proceeding {len(target_proceedings)}개 발견.")

                # [STEP 2] 연도별 Proceeding 진입 및 페이지네이션 처리
                for proc in target_proceedings:
                    current_y = proc['year']
                    base_proc_url = proc['url']
                    
                    # 1페이지 진입하여 전체 논문 수 파악
                    first_page_url = f"{base_proc_url}?rowsPerPage=100&pageNumber=1"
                    driver.get(first_page_url)
                    time.sleep(3)

                    # 전체 논문 수 추출 (예: "Showing 1-100 of 336")
                    total_papers = 100 # 기본값
                    try:
                        total_text_el = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "span[_ngcontent-ng-c412816549]")))
                        total_text = total_text_el.text # "Showing 1-100 of 336"
                        match = re.search(r'of\s+([\d,]+)', total_text)
                        if match:
                            total_papers = int(match.group(1).replace(',', ''))
                    except:
                        pass
                    
                    total_pages = math.ceil(total_papers / 100)
                    print(f"📅 {current_y}년: 총 {total_papers}개 논문 (약 {total_pages}페이지) 탐색 시작")

                    # 각 페이지 순회 수집
                    for page in range(1, total_pages + 1):
                        current_page = page
                        paged_url = f"{base_proc_url}?rowsPerPage=100&pageNumber={page}"
                        
                        for attempt in range(1, 4):
                            try:
                                print(f"🔎 {current_conf} {current_y} [Page {page}/{total_pages}] ({attempt}/3)...", end=" ", flush=True)
                                driver.get(paged_url)
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
                                                "Page": page, "Title": title_text, "Authors": author_text, "URL": link_url
                                            })
                                            count += 1
                                    except: continue
                                
                                print(f"✅ {count}개 완료")
                                break # 성공 시 재시도 루프 탈출
                            except:
                                time.sleep(10)

                    # 연도별 수집 후 즉시 중간 저장
                    if all_data:
                        pd.DataFrame(all_data).to_excel(output_path, index=False)

            except Exception as e:
                print(f"⚠️ [{current_conf}] 목록 분석 실패: {e}")
                continue

        print(f"\n✨ 학회 데이터 전수 조사 완주! 최종 파일: {output_path}")

    except Exception as e:
        print("\n" + "="*50)
        print(f"🔥 치명적 오류 발생 위치 파악")
        print(f"📍 학회: {current_conf} / 연도: {current_y} / 페이지: {current_page}")
        print(f"❌ 오류 내용: {e}")
        print("="*50)
    finally:
        driver.quit()

if __name__ == "__main__":
    sweep_ieee_conferences(2026, 2006)