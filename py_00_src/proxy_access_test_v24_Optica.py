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
        print("\n🚨 CAPTCHA 또는 접근 차단 감지!")
        input("✅ 가상 데스크톱에서 해결 후 [Enter]를 눌러주세요...")

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
    file_name = f"Optica_Final_Sweep_{now_str}.xlsx"
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
        time.sleep(random.uniform(3, 5))

        for journal in journals:
            print(f"\n📘 [{journal['name']}] 목록 스캔")
            driver.get(f"{proxy_prefix}https://opg.optica.org/{journal['id']}/browse.cfm")
            
            # [방어 로직 1] 연도 리스트를 텍스트 기반으로 미리 확보
            wait.until(EC.presence_of_all_elements_located((By.CLASS_NAME, "osap-accordion__label")))
            vol_labels = driver.find_elements(By.CLASS_NAME, "osap-accordion__label")
            target_year_texts = []
            for label in vol_labels:
                match = re.search(r'\((\d{4})\)', label.text)
                if match and end_year <= int(match.group(1)) <= start_year:
                    target_year_texts.append(label.text)

            # 확보한 텍스트로 루프를 돌며 요소를 매번 새로 찾음
            for y_text in target_year_texts:
                current_y = re.search(r'\((\d{4})\)', y_text).group(1)
                
                # [방어 로직 2] Stale Element 방지를 위해 클릭 시점에 요소를 다시 찾음
                try:
                    current_label = wait.until(EC.element_to_be_clickable((By.XPATH, f"//label[contains(text(), '{y_text}')]")))
                    driver.execute_script("arguments[0].click();", current_label)
                    time.sleep(random.uniform(1.0, 2.0))
                    
                    parent_div = current_label.find_element(By.XPATH, "./following-sibling::div")
                    issue_links = parent_div.find_elements(By.CSS_SELECTOR, "li.volume-issue-list__list-item a")
                    issue_tasks = [{"text": l.text, "url": l.get_attribute("href")} for l in issue_links if "issue.cfm" in l.get_attribute("href")]
                except StaleElementReferenceException:
                    print(f"🔄 {current_y}년 요소 유실, 재로딩합니다...")
                    continue # 다음 시도 시 다시 찾게 됨

                for task in issue_tasks:
                    print(f"🔎 {current_y} {task['text'][:12]}...", end=" ", flush=True)
                    
                    for attempt in range(1, 4):
                        try:
                            driver.get(task['url'])
                            check_captcha(driver)
                            wait.until(EC.presence_of_element_located((By.CLASS_NAME, "media-twbs-body")))
                            
                            # 아티클 수 확인 (v3 기능 복구)
                            expected_count = 0
                            try:
                                count_text = driver.find_element(By.CSS_SELECTOR, "h2.heading-block-header small").text
                                expected_count = int(re.search(r'(\d+)', count_text).group(1))
                            except: pass

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
                            
                            status = "✅" if count >= expected_count else "⚠️"
                            print(f"{status} {count}건")
                            time.sleep(random.uniform(0.5, 1.5))
                            break
                        except Exception:
                            if attempt == 3: print("❌ 실패")
                            driver.refresh()
                            time.sleep(5)
                    
                # 중간 저장
                if all_data:
                    pd.DataFrame(all_data).to_excel(output_path, index=False)

        print(f"\n✨ 수집 완주! 최종 파일: {output_path}")

    except Exception as e:
        print(f"\n🔥 치명적 에러: {e}")
    finally:
        driver.quit()

if __name__ == "__main__":
    sweep_optica_journals(2026, 2006)