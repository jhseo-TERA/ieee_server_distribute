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

def sweep_optica_journals(start_year=2026, end_year=2019, journals=None):
    if journals is None:
        journals = [
            {"name": "JLT", "id": "jlt"},
            {"name": "Optica", "id": "optica"},
            {"name": "PR", "id": "prj"},
            {"name": "OE", "id": "oe"},
            {"name": "OL", "id": "ol"}
        ]
    
    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = f"Optica_Sweep_{start_year}_{end_year}_{now_str}.xlsx"
    
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    output_path = os.path.join(base_dir, "py_01_data", "00_metadata", file_name)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    proxy_prefix = "https://access.yonsei.ac.kr/link.n2s?url="
    all_data = []
    options = Options()
    driver = webdriver.Edge(options=options)
    wait = WebDriverWait(driver, 25)

    current_j = "N/A"
    current_y = "N/A"
    current_i = "N/A"

    try:
        # [STEP 1] 로그인 및 세션 확보
        print(f"🔐 Optica 수집 시작 (ID/PW 자동 입력)...")
        driver.get(f"{proxy_prefix}https://opg.optica.org/jlt/browse.cfm")
        
        wait.until(EC.presence_of_element_located((By.ID, "id"))).send_keys(os.getenv("YONSEI_ID"))
        driver.find_element(By.ID, "password").send_keys(os.getenv("YONSEI_PW"))
        driver.find_element(By.XPATH, "//input[@type='submit' and @value='로그인']").click()
        time.sleep(5)

        for journal in journals:
            current_j = journal['name']
            print(f"\n📘 [{current_j}] Browse All Issues 분석 중")
            
            browse_url = f"{proxy_prefix}https://opg.optica.org/{journal['id']}/browse.cfm"
            driver.get(browse_url)
            
            try:
                # [STEP 2] Volume (연도) 아코디언 분석
                # 제공된 HTML: label.osap-accordion__label
                wait.until(EC.presence_of_element_located((By.CLASS_NAME, "osap-accordion__label")))
                vol_labels = driver.find_elements(By.CLASS_NAME, "osap-accordion__label")
                
                target_volumes = []
                for label in vol_labels:
                    txt = label.text # 예: "Vol. 44 (2026)"
                    match = re.search(r'\((\d{4})\)', txt)
                    if match:
                        y = int(match.group(1))
                        if end_year <= y <= start_year:
                            # 아코디언 클릭용 ID 또는 label 객체 저장
                            target_volumes.append({"year": y, "element": label})
                
                print(f"✅ {len(target_volumes)}개 연도 탐색 대상 발견.")

                for vol in target_volumes:
                    current_y = vol['year']
                    # 아코디언 클릭해서 이슈 목록 펼치기
                    driver.execute_script("arguments[0].click();", vol['element'])
                    time.sleep(1)
                    
                    # [STEP 3] Issue (호) 링크 추출
                    # 제공된 HTML: li.volume-issue-list__list-item a
                    # 현재 펼쳐진 연도 블록 내부의 이슈들만 찾기 위해 부모 요소를 거쳐 탐색
                    parent_div = vol['element'].find_element(By.XPATH, "./following-sibling::div")
                    issue_links = parent_div.find_elements(By.CSS_SELECTOR, "li.volume-issue-list__list-item a")
                    issue_tasks = [{"text": l.text, "url": l.get_attribute("href")} for l in issue_links]
                    
                    print(f"📅 {current_y}년: {len(issue_tasks)}개 이슈 발견")

                    for task in issue_tasks:
                        current_i = task['text']
                        
                        for attempt in range(1, 4):
                            try:
                                driver.get(task['url'])
                                if attempt > 1: driver.refresh()
                                
                                # [STEP 4] 상단 Article 수 파악 (검증용)
                                # 제공된 HTML: h2.heading-block-header small
                                expected_count = 0
                                try:
                                    count_el = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "h2.heading-block-header small")))
                                    count_text = count_el.text # 예: "45 articles"
                                    expected_count = int(re.search(r'(\d+)', count_text).group(1))
                                except: pass

                                print(f"  🔎 {current_y} {current_i} (목표: {expected_count}건)...", end=" ", flush=True)
                                
                                # 논문 리스트 로드 대기
                                wait.until(EC.presence_of_element_located((By.CLASS_NAME, "media-body")))
                                articles = driver.find_elements(By.CLASS_NAME, "media-body")
                                
                                count = 0
                                for art in articles:
                                    try:
                                        title_el = art.find_element(By.CSS_SELECTOR, "h5 a")
                                        title_text = clean_text(title_el.text)
                                        paper_url = title_el.get_attribute("href")
                                        
                                        try:
                                            author_text = clean_text(art.find_element(By.CSS_SELECTOR, ".media-text, p").text)
                                        except: author_text = "N/A"
                                        
                                        if title_text and "/abstract.cfm" in paper_url:
                                            all_data.append({
                                                "Journal": current_j, "Year": current_y, "Issue": current_i,
                                                "Title": title_text, "Authors": author_text, "URL": paper_url
                                            })
                                            count += 1
                                    except: continue
                                
                                # 검증 로그 출력
                                status = "✅" if count >= expected_count else "⚠️ 미달"
                                print(f"{status} {count}건 완료")
                                break
                            except:
                                time.sleep(5)
                    
                    # 연도별 중간 저장
                    if all_data:
                        pd.DataFrame(all_data).to_excel(output_path, index=False)

            except Exception as e:
                print(f"⚠️ [{current_j}] 목록 처리 중 오류: {e}")
                continue

        print(f"\n✨ Optica 수집 완주! 최종 파일: {output_path}")

    except Exception as e:
        print(f"\n" + "="*50)
        print(f"🔥 치명적 오류 발생")
        print(f"📍 위치: {current_j} / {current_y} / {current_i}")
        print(f"❌ 내용: {e}")
        print("="*50)
    finally:
        driver.quit()

if __name__ == "__main__":
    # 2026년부터 2006년까지 전수 조사
    sweep_optica_journals(2026, 2019)