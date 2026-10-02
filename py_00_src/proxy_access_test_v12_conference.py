import os
import time
import re
import pandas as pd
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

def sweep_ieee_proceedings(start_year=2026, end_year=2020, conferences=None):
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

    # 파일명 자동 생성 로직
    conf_names_str = "_".join([c['name'] for c in conferences[:3]]) + f"_and_{len(conferences)-3}_others"
    file_name = f"IEEE_Conf_Sweep_{start_year}_{end_year}_{conf_names_str}.xlsx"
    
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    output_path = os.path.join(base_dir, "py_01_data", "00_metadata", file_name)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    proxy_prefix = "https://access.yonsei.ac.kr/link.n2s?url="
    all_data = []
    options = Options()
    driver = webdriver.Edge(options=options)
    wait = WebDriverWait(driver, 25)

    try:
        # [STEP 1] 로그인
        print(f"🔐 학회 데이터 수집 시작 (파일명: {file_name})")
        driver.get(f"{proxy_prefix}https://ieeexplore.ieee.org/xpl/conhome/1000708/all-proceedings")
        wait.until(EC.presence_of_element_located((By.ID, "id"))).send_keys(os.getenv("YONSEI_ID"))
        driver.find_element(By.ID, "password").send_keys(os.getenv("YONSEI_PW"))
        driver.find_element(By.XPATH, "//input[@type='submit' and @value='로그인']").click()
        time.sleep(5)

        for conf in conferences:
            print(f"\n🚀 [{conf['name']}] All Proceedings 목록 분석 중...")
            all_proc_url = f"{proxy_prefix}https://ieeexplore.ieee.org/xpl/conhome/{conf['id']}/all-proceedings"
            driver.get(all_proc_url)
            
            try:
                # 준혁님이 주신 HTML 구조: <li> <h2> <a> (연도 포함 텍스트) 추출
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "li h2 a")))
                time.sleep(2)
                
                proc_links = driver.find_elements(By.CSS_SELECTOR, "li h2 a")
                target_proceedings = []
                
                for link in proc_links:
                    link_text = link.text
                    link_href = link.get_attribute("href")
                    
                    # 텍스트에서 20xx 연도 추출
                    year_match = re.search(r'20\d{2}', link_text)
                    if year_match:
                        year_val = int(year_match.group())
                        if end_year <= year_val <= start_year:
                            target_proceedings.append({"year": year_val, "url": link_href})
                
                print(f"✅ {start_year}-{end_year} 구간 Proceeding {len(target_proceedings)}개 발견")

                # [STEP 2] 개별 Proceeding 접속 및 논문 수집
                for proc in target_proceedings:
                    year = proc["year"]
                    paged_url = f"{proc['url']}&rowsPerPage=100" # 한 페이지에 모든 논문 출력
                    
                    for attempt in range(3):
                        try:
                            print(f"🔎 {conf['name']} {year} 진입 (시도 {attempt+1}/3)...", end=" ", flush=True)
                            driver.get(paged_url)
                            wait.until(EC.presence_of_element_located((By.CLASS_NAME, "List-results-items")))
                            time.sleep(3)

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
                                            "Conference": conf['name'],
                                            "Year": year,
                                            "Title": title_text,
                                            "Authors": author_text,
                                            "URL": link_url
                                        })
                                        count += 1
                                except: continue
                            
                            print(f"✅ {count}개 수집 완료")
                            break
                        except:
                            time.sleep(5)
                            continue
                    
                    # 실시간 중간 저장
                    if all_data:
                        pd.DataFrame(all_data).to_excel(output_path, index=False)
                        
            except Exception as e:
                print(f"⚠️ [{conf['name']}] 목록 로딩 실패. 패스합니다.")
                continue

    except Exception as e: print(f"🔥 치명적 오류: {e}")
    finally:
        driver.quit()
        print(f"\n✨ 학회 DB 구축 완료! 최종 파일: {output_path}")

if __name__ == "__main__":
    sweep_ieee_proceedings(2026, 2020)