import os
import time
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

def sweep_ieee_target_range(start_year=2026, end_year=2019, journals=None):
    if journals is None:
        journals = [
            {"name": "JSSC", "id": "4"},
            {"name": "TCAS-I", "id": "8919"},
            {"name": "TCAS-II", "id": "8920"}
        ]
    
    journal_names_str = "_".join([j['name'] for j in journals])
    file_name = f"IEEE_Sweep_{start_year}_{end_year}_{journal_names_str}.xlsx"
    
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
        print(f"🔐 로그인 중... (대상: {start_year}~{end_year})")
        driver.get(f"{proxy_prefix}https://ieeexplore.ieee.org/xpl/issues?punumber=4")
        wait.until(EC.presence_of_element_located((By.ID, "id"))).send_keys(os.getenv("YONSEI_ID"))
        driver.find_element(By.ID, "password").send_keys(os.getenv("YONSEI_PW"))
        driver.find_element(By.XPATH, "//input[@type='submit' and @value='로그인']").click()
        time.sleep(5)

        for journal in journals:
            print(f"\n📘 [{journal['name']}] 스캔 시작")
            
            for year in range(start_year, end_year - 1, -1):
                year_url = f"{proxy_prefix}https://ieeexplore.ieee.org/xpl/issues?punumber={journal['id']}&isyear={year}"
                driver.get(year_url)
                
                try:
                    # [JSSC 특화 로직] 연도 버튼이 안 보이면 Decade(예: 2010s) 클릭
                    year_xpath = f"//a[text()='{year}']"
                    
                    try:
                        # 1. 연도 버튼이 즉시 클릭 가능한지 확인
                        year_button = wait.until(EC.element_to_be_clickable((By.XPATH, year_xpath)))
                    except:
                        # 2. 안 보인다면 Decade 버튼(예: 2010s) 찾기
                        decade_str = f"{str(year // 10 * 10)}s"
                        print(f"📂 {year}년이 숨겨져 있음. {decade_str} 탭을 엽니다.")
                        decade_xpath = f"//a[text()='{decade_str}']"
                        decade_button = wait.until(EC.element_to_be_clickable((By.XPATH, decade_xpath)))
                        driver.execute_script("arguments[0].click();", decade_button)
                        time.sleep(2)
                        # 3. 다시 연도 버튼 찾기
                        year_button = wait.until(EC.element_to_be_clickable((By.XPATH, year_xpath)))

                    # 연도 버튼의 부모 li가 active가 아니면 클릭
                    parent_li = year_button.find_element(By.XPATH, "./parent::li")
                    if "active" not in parent_li.get_attribute("class"):
                        driver.execute_script("arguments[0].click();", year_button)
                        time.sleep(3)

                    # 이슈 목록 가져오기
                    issue_elements = driver.find_elements(By.PARTIAL_LINK_TEXT, "Issue ")
                    issue_info = [{"text": el.text, "url": el.get_attribute("href")} for el in issue_elements]
                except Exception as e:
                    print(f"⚠️ {year}년 로딩 실패. 패스합니다.")
                    continue

                for info in issue_info:
                    issue_text = info["text"]
                    paged_url = f"{info['url']}&rowsPerPage=100"
                    
                    for attempt in range(3):
                        try:
                            print(f"🔎 {journal['name']} {year} {issue_text} ({attempt+1}/3)...", end=" ", flush=True)
                            driver.get(paged_url)
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
                                            "Journal": journal['name'], "Year": year, "Issue": issue_text,
                                            "Title": title_text, "Authors": author_text, "URL": link_url
                                        })
                                        count += 1
                                except: continue
                            print(f"✅ {count}개")
                            break
                        except:
                            time.sleep(5)
                            continue

                    driver.get(year_url)
                
                # 연도별 중간 저장
                if all_data:
                    pd.DataFrame(all_data).to_excel(output_path, index=False)

        print(f"\n✨ 수집 완료! 최종 파일: {output_path}")

    except Exception as e: print(f"🔥 오류: {e}")
    finally: driver.quit()

if __name__ == "__main__":
    sweep_ieee_target_range(2026, 2019)