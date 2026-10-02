import os
import sys
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
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

load_dotenv()

def clean_text(text):
    if not text: return ""
    return ' '.join(text.replace('\n', ' ').replace('\r', ' ').split())

def check_captcha(driver):
    """실제 차단 문구를 기반으로 한 정밀 탐지.
    driver.get() 직후 리다이렉트가 아직 끝나지 않은 순간에 title/page_source를
    읽으면 일시적인 WebDriverException("unable to determine loading status" 등,
    메시지가 비어있는 경우가 많음)이 날 수 있어, 여기서는 그런 경우를 차단으로
    오판하지 않고 그냥 "아직 판단 불가 -> 통과"로 넘어간다(호출부에서 재시도됨)."""
    try:
        current_title = driver.title.lower()
    except Exception:
        return
    blocked_titles = ["access denied", "robot", "cloudflare", "distil", "attention required", "security check"]
    is_blocked = any(bt in current_title for bt in blocked_titles)

    if not is_blocked:
        # 준혁 님이 제공해주신 실제 차단 페이지 핵심 문구
        block_keywords = [
            "we apologize for the inconvenience",
            "activity and behavior on this site",
            "made us think that you are a bot",
            "malicious behavior"
        ]
        try:
            page_content = driver.page_source.lower()
        except Exception:
            return
        if any(kw in page_content for kw in block_keywords):
            is_blocked = True

    if is_blocked:
        print("\n" + "!"*60)
        print("🚨 [차단 감지] Optica 보안 시스템이 작동 중입니다!")
        print(f"📍 상태: {driver.title}")
        print("💡 가상 데스크톱 브라우저 창에서 캡차를 풀고 목록이 나오게 하세요.")
        print("💡 해결 전까지 CMD에서 엔터를 누르지 마세요.")
        print("!"*60)
        
        input("✅ 해결 완료(논문 목록 확인) 후 여기서 [Enter]를 누르세요...")
        driver.refresh()
        time.sleep(3)
        check_captcha(driver) # 해결 여부 재검사

def sweep_optica_journals(start_year=2026, end_year=2019, journals=None):
    if journals is None:
        journals = [
            {"name": "PR", "id": "prj"},
            {"name": "JLT", "id": "jlt"},
            {"name": "Optica", "id": "optica"},
            {"name": "OE", "id": "oe"},
            {"name": "OL", "id": "ol"}
        ]
    
    print("\n" + "="*50)
    print(f"📡 Optica 수집 시스템 가동 (v29.1_Full)")
    print(f"📅 목표 범위: {start_year}년 ~ {end_year}년")
    print("="*50 + "\n")

    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = f"Optica_Sweep_{now_str}.xlsx"
    output_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "py_01_data", "00_metadata", file_name)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    options = Options()
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    
    driver = webdriver.Edge(options=options)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    wait = WebDriverWait(driver, 15)
    proxy_prefix = "https://access.yonsei.ac.kr/link.n2s?url="
    all_data = []

    try:
        # [로그인]
        driver.get(f"{proxy_prefix}https://opg.optica.org/prj/browse.cfm")
        wait.until(EC.presence_of_element_located((By.ID, "id"))).send_keys(os.getenv("YONSEI_ID"))
        driver.find_element(By.ID, "password").send_keys(os.getenv("YONSEI_PW"))
        driver.find_element(By.XPATH, "//input[@type='submit' and @value='로그인']").click()
        time.sleep(5)

        for journal in journals:
          try:
            print(f"📘 [{journal['name']}] 목록 확보 중...")
            driver.get(f"{proxy_prefix}https://opg.optica.org/{journal['id']}/browse.cfm")
            time.sleep(2)  # 프록시 리다이렉트가 끝나기 전에 title/page_source 를 읽어
                           # 빈 메시지의 WebDriverException 이 나는 것을 방지
            check_captcha(driver)

            # https://translate.google.co.kr/?hl=en&tl=ko
            wait.until(EC.presence_of_all_elements_located((By.CLASS_NAME, "osap-accordion__label")))
            labels = driver.find_elements(By.CLASS_NAME, "osap-accordion__label")

            all_issue_tasks = []
            for label in labels:
                try:
                    match = re.search(r'\((\d{4})\)', label.text)
                    if match:
                        year = int(match.group(1))
                        if not (end_year <= year <= start_year):
                            continue

                        driver.execute_script("arguments[0].click();", label)
                        time.sleep(0.5)

                        parent_div = label.find_element(By.XPATH, "./following-sibling::div")
                        links = parent_div.find_elements(By.CSS_SELECTOR, "li.volume-issue-list__list-item a")

                        for l in links:
                            url = l.get_attribute("href")
                            txt = l.text
                            if "upcomingissue.cfm" in url or "Issues in Progress" in txt:
                                continue
                            if "issue.cfm" in url:
                                all_issue_tasks.append({"year": year, "issue_text": txt, "url": url})
                except StaleElementReferenceException:
                    continue
                except Exception as e:
                    print(f"  [주의] 라벨 처리 중 오류(건너뜀): {type(e).__name__}: {e}")
                    continue

            print(f"✅ [{journal['name']}] 총 {len(all_issue_tasks)}개 이슈 확보됨.")

            # [본 수집 로직]
            for task in all_issue_tasks:
                success = False
                while not success: # 수집 성공 전까지 무한 루프 (차단 시 대기)
                    try:
                        driver.get(task['url'])
                        time.sleep(1)
                        check_captcha(driver)

                        try:
                            # 논문 리스트가 뜰 때까지 대기
                            wait.until(EC.presence_of_element_located((By.CLASS_NAME, "media-twbs-body")))
                        except TimeoutException:
                            # 못 찾으면 캡차 재검사
                            check_captcha(driver)
                            # 캡차 페이지가 아닌데도 없으면 정말 데이터가 없는 것
                            if "apologize" not in driver.page_source.lower():
                                print(f"🔎 {journal['name']} {task['year']} {task['issue_text'][:10]}... ⚠️ 데이터 없음(Skip)")
                                success = True
                                break
                            else: continue # 차단이면 다시 시도

                        # 정상 파싱
                        count_text = driver.find_element(By.CSS_SELECTOR, "h2.heading-block-header small").text
                        expected_count = int(re.search(r'(\d+)', count_text).group(1))
                        print(f"🔎 {journal['name']} {task['year']} {task['issue_text'][:10]} ({expected_count}건)...", end=" ", flush=True)

                        all_items = driver.find_elements(By.CSS_SELECTOR, "h2.toc-header--level3, div.media-twbs-body")
                        count = 0
                        current_category = "General"
                        for item in all_items:
                            if "toc-header--level3" in item.get_attribute("class"):
                                current_category = clean_text(item.text)
                            else:
                                try:
                                    title_el = item.find_element(By.CSS_SELECTOR, "p.article-title a")
                                    author_el = item.find_element(By.CSS_SELECTOR, "p.article-authors")
                                    all_data.append({
                                        "Journal": journal['name'], "Year": task['year'], "Category": current_category,
                                        "Title": clean_text(title_el.text), "Authors": clean_text(author_el.text),
                                        "URL": title_el.get_attribute("href")
                                    })
                                    count += 1
                                except: continue

                        print(f"✅ {count}건 완료")
                        success = True
                        time.sleep(random.uniform(1.0, 2.5))
                    except Exception as e:
                        print(f"\n🔄 재시도 중... ({type(e).__name__}: {e})")
                        driver.refresh()
                        time.sleep(5)

                # 이슈 하나 끝날 때마다 저장(중간에 죽어도 유실 최소화).
                # 예전 "len % 50 == 0" 조건은 한 이슈당 100건씩 뭉텅이로 들어와
                # 누적 합계가 정확히 50의 배수가 되는 경우가 거의 없어 사실상
                # 저장이 안 되는 문제가 있었음.
                if all_data:
                    pd.DataFrame(all_data).to_excel(output_path, index=False)

          except Exception as e:
            # 저널 하나에서 문제가 생겨도 나머지 저널은 계속 진행
            print(f"\n⚠️ [{journal['name']}] 처리 중 오류로 이 저널은 건너뜁니다: {type(e).__name__}: {e}")
            if all_data:
                pd.DataFrame(all_data).to_excel(output_path, index=False)
            continue

        print(f"\n✨ 모든 저널 수집 완료! 파일: {output_path}")

    except Exception as e:
        print(f"\n🔥 치명적 에러: {type(e).__name__}: {e}")
    finally:
        if all_data:
            pd.DataFrame(all_data).to_excel(output_path, index=False)
        driver.quit()

if __name__ == "__main__":
    sweep_optica_journals(2026, 2019)