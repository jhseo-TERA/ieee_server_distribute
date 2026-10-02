import os
import time
import re
import random
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException

load_dotenv()

def clean_text(text):
    if not text: return ""
    return ' '.join(text.replace('\n', ' ').replace('\r', ' ').split())

def check_captcha(driver):
    captcha_keywords = ["captcha", "robot", "challenge", "distil", "denied"]
    if any(kw in driver.page_source.lower() for kw in captcha_keywords):
        print("\n🚨 CAPTCHA 감지! 가상 데스크톱에서 해결 후 [Enter]를 눌러주세요...")
        input()

def sweep_optica_journals(start_year=2026, end_year=2006, journals=None):
    # [v8.1 반영] 수집 범위 및 대상 학회 정보 출력
    if journals is None:
        journals = [
            {"name": "PR", "id": "prj"},
            {"name": "JLT", "id": "jlt"},
            {"name": "Optica", "id": "optica"},
            {"name": "OE", "id": "oe"},
            {"name": "OL", "id": "ol"}
        ]
    
    print("\n" + "="*50)
    print(f"📡 Optica 수집 시스템 가동 (v25_Integrated)")
    print(f"📅 목표 범위: {start_year}년 부터 {end_year}년 까지")
    print(f"📝 대상 학회: {[j['name'] for j in journals]}")
    print("="*50 + "\n")

    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = f"Optica_Sweep_{start_year}_{end_year}_{now_str}.xlsx"
    output_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "py_01_data", "00_metadata", file_name)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    options = Options()
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    
    driver = webdriver.Edge(options=options)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    wait = WebDriverWait(driver, 20)
    proxy_prefix = "https://access.yonsei.ac.kr/link.n2s?url="
    all_data = []

    try:
        # [로그인 세션 확보]
        driver.get(f"{proxy_prefix}https://opg.optica.org/prj/browse.cfm")
        wait.until(EC.presence_of_element_located((By.ID, "id"))).send_keys(os.getenv("YONSEI_ID"))
        driver.find_element(By.ID, "password").send_keys(os.getenv("YONSEI_PW"))
        driver.find_element(By.XPATH, "//input[@type='submit' and @value='로그인']").click()
        time.sleep(5)

        for journal in journals:
            print(f"📘 [{journal['name']}] 수집 대상(Issue URL) 확보 중...")
            driver.get(f"{proxy_prefix}https://opg.optica.org/{journal['id']}/browse.cfm")
            
            wait.until(EC.presence_of_all_elements_located((By.CLASS_NAME, "osap-accordion__label")))
            labels = driver.find_elements(By.CLASS_NAME, "osap-accordion__label")
            
            all_issue_tasks = []
            for label in labels:
                try:
                    match = re.search(r'\((\d{4})\)', label.text)
                    if match:
                        year = int(match.group(1))
                        # [v8.1 반영] 연도 범위가 맞지 않으면 리스트 생성 단계에서 제외
                        if not (end_year <= year <= start_year):
                            continue

                        driver.execute_script("arguments[0].click();", label)
                        time.sleep(0.5)
                        
                        parent_div = label.find_element(By.XPATH, "./following-sibling::div")
                        links = parent_div.find_elements(By.CSS_SELECTOR, "li.volume-issue-list__list-item a")
                        
                        for l in links:
                            url = l.get_attribute("href")
                            txt = l.text
                            
                            # [v8 반영] Upcoming Issue 및 Issues in Progress 링크 제외
                            if "upcomingissue.cfm" in url or "Issues in Progress" in txt:
                                continue
                                
                            if "issue.cfm" in url:
                                all_issue_tasks.append({"year": year, "issue_text": txt, "url": url})
                except StaleElementReferenceException:
                    continue

            print(f"✅ [{journal['name']}] 총 {len(all_issue_tasks)}개의 정식 이슈 확보. 수집 시작.")

            for task in all_issue_tasks:
                # [v8.1 반영] 수집 직전 한 번 더 연도 범위 검증
                if not (end_year <= task['year'] <= start_year):
                    continue

                current_y = task['year']
                current_i = task['issue_text']
                
                for attempt in range(1, 4):
                    try:
                        driver.get(task['url'])
                        check_captcha(driver)
                        
                        try:
                            wait.until(EC.presence_of_element_located((By.CLASS_NAME, "media-twbs-body")))
                        except:
                            print(f"🔎 {journal['name']} {current_y} {current_i[:10]}... ⚠️ 논문 없음(Skip)")
                            break

                        expected_count = 0
                        try:
                            count_text = driver.find_element(By.CSS_SELECTOR, "h2.heading-block-header small").text
                            expected_count = int(re.search(r'(\d+)', count_text).group(1))
                        except: pass

                        print(f"🔎 {journal['name']} {current_y} {current_i[:10]} (목표:{expected_count})...", end=" ", flush=True)

                        current_category = "General"
                        all_items = driver.find_elements(By.CSS_SELECTOR, "h2.toc-header--level3, div.media-twbs-body")
                        
                        count = 0
                        for item in all_items:
                            if "toc-header--level3" in item.get_attribute("class"):
                                current_category = clean_text(item.text)
                            else:
                                try:
                                    title_el = item.find_element(By.CSS_SELECTOR, "p.article-title a")
                                    author_el = item.find_element(By.CSS_SELECTOR, "p.article-authors")
                                    all_data.append({
                                        "Journal": journal['name'], "Year": current_y, "Category": current_category,
                                        "Title": clean_text(title_el.text), "Authors": clean_text(author_el.text),
                                        "URL": title_el.get_attribute("href")
                                    })
                                    count += 1
                                except: continue
                        
                        print(f"✅ {count}건 완료")
                        time.sleep(random.uniform(1.0, 2.0))
                        break
                    except Exception:
                        if attempt == 3: print("❌ 오류 발생(건너뜀)")
                        driver.refresh()
                        time.sleep(5)
                
                if len(all_data) % 50 == 0:
                    pd.DataFrame(all_data).to_excel(output_path, index=False)

        print(f"\n✨ 수집 완료! 최종 파일: {output_path}")

    except Exception as e:
        print(f"\n🔥 치명적 에러: {e}")
    finally:
        driver.quit()

if __name__ == "__main__":
    # 목표하신 2026년부터 2019년까지로 설정되어 있습니다.
    sweep_optica_journals(2026, 2019)