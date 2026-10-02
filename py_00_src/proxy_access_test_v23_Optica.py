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

load_dotenv()

def clean_text(text):
    if not text: return ""
    return ' '.join(text.replace('\n', ' ').replace('\r', ' ').split())

def check_captcha(driver):
    """캡차 발생 시 사용자가 직접 풀 때까지 대기하는 함수"""
    captcha_keywords = ["captcha", "robot", "challenge", "distil"]
    if any(kw in driver.page_source.lower() for kw in captcha_keywords):
        print("\n🚨 CAPTCHA(봇 탐지)가 감지되었습니다!")
        print("💡 가상 데스크톱 화면에서 캡차를 직접 해결해 주세요.")
        input("✅ 해결 완료 후 여기서 [Enter]를 누르면 수집을 재개합니다...")
        time.sleep(2)

def sweep_optica_journals(start_year=2026, end_year=2006, journals=None):
    if journals is None:
        journals = [
            {"name": "PR", "id": "prj"},
            {"name": "JLT", "id": "jlt"},
            {"name": "Optica", "id": "optica"},
            {"name": "OE", "id": "oe"},
            {"name": "OL", "id": "ol"}
        ]
    
    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = f"Optica_Stealth_Sweep_{now_str}.xlsx"
    
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    output_path = os.path.join(base_dir, "py_01_data", "00_metadata", file_name)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # --- [스텔스 설정 시작] ---
    options = Options()
    # 자동화 제어 플래그 숨기기
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    # 일반적인 User-Agent 설정
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    
    driver = webdriver.Edge(options=options)
    # 브라우저 내부 플래그 강제 제거
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    wait = WebDriverWait(driver, 30)

    proxy_prefix = "https://access.yonsei.ac.kr/link.n2s?url="
    all_data = []

    try:
        # [로그인]
        print(f"🔐 세션 확보 및 로그인 시작...")
        driver.get(f"{proxy_prefix}https://opg.optica.org/prj/browse.cfm")
        check_captcha(driver)
        
        wait.until(EC.presence_of_element_located((By.ID, "id"))).send_keys(os.getenv("YONSEI_ID"))
        driver.find_element(By.ID, "password").send_keys(os.getenv("YONSEI_PW"))
        driver.find_element(By.XPATH, "//input[@type='submit' and @value='로그인']").click()
        time.sleep(random.uniform(5, 7))

        for journal in journals:
            print(f"\n📘 [{journal['name']}] 수집 시작")
            driver.get(f"{proxy_prefix}https://opg.optica.org/{journal['id']}/browse.cfm")
            check_captcha(driver)
            
            wait.until(EC.presence_of_element_located((By.CLASS_NAME, "osap-accordion__label")))
            vol_labels = driver.find_elements(By.CLASS_NAME, "osap-accordion__label")
            
            target_volumes = []
            for label in vol_labels:
                match = re.search(r'\((\d{4})\)', label.text)
                if match and end_year <= int(match.group(1)) <= start_year:
                    target_volumes.append({"year": int(match.group(1)), "element": label})

            for vol in target_volumes:
                print(f"📅 {vol['year']}년 이슈 목록 전개 중...")
                driver.execute_script("arguments[0].click();", vol['element'])
                time.sleep(random.uniform(1.5, 3.0)) # 사람 같은 간격
                
                parent_div = vol['element'].find_element(By.XPATH, "./following-sibling::div")
                issue_links = parent_div.find_elements(By.CSS_SELECTOR, "li.volume-issue-list__list-item a")
                issue_urls = [l.get_attribute("href") for l in issue_links if "issue.cfm" in l.get_attribute("href")]

                for issue_url in issue_urls:
                    for attempt in range(1, 4):
                        try:
                            driver.get(issue_url)
                            check_captcha(driver)
                            time.sleep(random.uniform(3, 5)) # 로딩 랜덤 대기

                            # [PR 특화 파싱 로직]
                            # 페이지 내의 모든 요소를 순회하며 카테고리와 논문 매칭
                            container = wait.until(EC.presence_of_element_located((By.ID, "article-list")))
                            elements = container.find_elements(By.XPATH, "./div | ./h2 | .//h2 | .//div[contains(@class, 'media-twbs-body')]")
                            
                            current_category = "General"
                            count = 0
                            
                            # 모든 자식 요소를 돌며 Category와 Article 구분 수집
                            # (h2가 나오면 카테고리 업데이트, media-twbs-body가 나오면 데이터 저장)
                            all_items = driver.find_elements(By.CSS_SELECTOR, "h2.toc-header--level3, div.media-twbs-body")
                            
                            for item in all_items:
                                if item.tag_name == "h2":
                                    current_category = clean_text(item.text)
                                elif item.tag_name == "div":
                                    try:
                                        title_el = item.find_element(By.CSS_SELECTOR, "p.article-title a")
                                        author_el = item.find_element(By.CSS_SELECTOR, "p.article-authors")
                                        
                                        all_data.append({
                                            "Journal": journal['name'],
                                            "Year": vol['year'],
                                            "Category": current_category,
                                            "Title": clean_text(title_el.text),
                                            "Authors": clean_text(author_el.text),
                                            "URL": title_el.get_attribute("href")
                                        })
                                        count += 1
                                    except: continue
                            
                            print(f"  🔎 {vol['year']} Issue: ✅ {count}건 (Cat: {current_category[:15]}...)")
                            break
                        except:
                            time.sleep(random.uniform(5, 10))
                            driver.refresh()
                    
                    # 중간 저장
                    if all_data:
                        pd.DataFrame(all_data).to_excel(output_path, index=False)

        print(f"\n✨ Optica 스텔스 수집 완료! 파일: {output_path}")

    except Exception as e:
        print(f"\n🔥 치명적 오류: {e}")
    finally:
        driver.quit()

if __name__ == "__main__":
    sweep_optica_journals(2026, 2006)