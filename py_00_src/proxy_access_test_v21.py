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

    try:
        # [STEP 1] 로그인
        print(f"🔐 로그인 및 세션 확보 중...")
        driver.get(f"{proxy_prefix}https://ieeexplore.ieee.org/xpl/conhome/1000708/all-proceedings")
        wait.until(EC.presence_of_element_located((By.ID, "id"))).send_keys(os.getenv("YONSEI_ID"))
        driver.find_element(By.ID, "password").send_keys(os.getenv("YONSEI_PW"))
        driver.find_element(By.XPATH, "//input[@type='submit' and @value='로그인']").click()
        time.sleep(5)

        for conf in conferences:
            current_conf = conf['name']
            print(f"\n🚀 [{current_conf}] All Proceedings 목록 스캔")
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
                
                for proc in target_proceedings:
                    current_y = proc['year']
                    base_proc_url = proc['url']
                    
                    # 1페이지 진입
                    first_page_url = f"{base_proc_url}?rowsPerPage=100&pageNumber=1"
                    driver.get(first_page_url)
                    
                    # [v22 핵심 수정] 제공된 HTML 구조 기반의 정밀 파싱
                    total_papers = 100
                    try:
                        # Dashboard-header 클래스가 나타날 때까지 대기
                        wait.until(EC.presence_of_element_located((By.CLASS_NAME, "Dashboard-header")))
                        header_el = driver.find_element(By.CLASS_NAME, "Dashboard-header")
                        
                        # 내부의 'strong' 태그들을 모두 찾음 (1-100과 전체 개수)
                        strong_elements = header_el.find_elements(By.CLASS_NAME, "strong")
                        if len(strong_elements) >= 2:
                            # 마지막 strong 태그의 텍스트가 전체 개수
                            total_papers = int(strong_elements[-1].text.replace(',', ''))
                        else:
                            # 1페이지뿐인 경우 전체 텍스트에서 파싱 시도
                            match = re.search(r'of\s+([\d,]+)', header_el.text)
                            if match:
                                total_papers = int(match.group(1).replace(',', ''))
                    except Exception as e:
                        print(f"⚠️ {current_y}년 파싱 지연... (자바스크립트 재시도)")
                        # Fallback: JavaScript로 직접 값 추출
                        try:
                            total_papers = int(driver.execute_script(
                                "return document.querySelector('.Dashboard-header').querySelectorAll('.strong')[1].innerText;"
                            ).replace(',', ''))
                        except: pass
                    
                    total_pages = math.ceil(total_papers / 100)
                    print(f"📅 {current_y}년: 실제 총 {total_papers}개 논문 ({total_pages}페이지) 감지됨")

                    # [STEP 3] 각 페이지 순회 수집
                    for page in range(1, total_pages + 1):
                        paged_url = f"{base_proc_url}?rowsPerPage=100&pageNumber={page}"
                        
                        for attempt in range(1, 4):
                            try:
                                print(f"🔎 {current_conf} {current_y} [P{page}/{total_pages}] ({attempt}/3)...", end=" ", flush=True)
                                driver.get(paged_url)
                                if attempt > 1: 
                                    driver.refresh()
                                    time.sleep(5)
                                
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
                                
                                print(f"✅ {count}개")
                                break
                            except:
                                time.sleep(10)

                    if all_data:
                        pd.DataFrame(all_data).to_excel(output_path, index=False)

            except Exception as e:
                print(f"⚠️ [{current_conf}] 분석 실패: {e}")
                continue

        print(f"\n✨ 수집 완료! 파일: {output_path}")

    except Exception as e:
        print(f"\n🔥 치명적 오류 발생 위치: {current_conf}/{current_y}\n❌ {e}")
    finally:
        driver.quit()

if __name__ == "__main__":
    sweep_ieee_conferences(2026, 2006)