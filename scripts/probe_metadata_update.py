"""Read-only operational probes; output contains no credentials or cookies."""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.fetch_ieee_crossref import CrossrefClient
from scripts.metadata_sources import sources, select_sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--crossref', action='store_true')
    parser.add_argument('--db', action='store_true')
    parser.add_argument('--source', help='Limit Crossref probe to a registry source')
    parser.add_argument('--year', type=int)
    parser.add_argument('--browser', choices=['journal', 'conference'])
    args = parser.parse_args()
    report = {'time': datetime.now().isoformat()}
    if args.crossref:
        client = CrossrefClient()
        report['crossref'] = {}
        items = select_sources(sources(collector='ieee_crossref'), args.source)
        for config in items:
            name = config['name']
            journal = config['type'] == 'journal'
            query = config['issn'] if journal else config['query']
            high, low = (args.year, args.year) if args.year else (datetime.now().year, datetime.now().year - 1)
            params = {'rows': 30, 'filter': f'from-pub-date:{low}-01-01,until-pub-date:{high}-12-31',
                      'select': 'DOI,container-title,ISSN,resource'}
            if not journal:
                params['query.container-title'] = query
                params['filter'] += ',type:proceedings-article,' + ','.join(
                    f'prefix:{prefix}' for prefix in config.get('doi_prefixes', ['10.1109']))
            data = client.get_message(f'/journals/{query}/works' if journal else '/works', params)
            titles = {}
            for item in data['items']:
                for title in item.get('container-title', []):
                    titles.setdefault(title, item.get('DOI'))
            report['crossref'][name] = {'candidate_total': data['total-results'], 'titles': titles}
            print(name, json.dumps(report['crossref'][name], ensure_ascii=False), flush=True)
    if args.db:
        import pymysql
        from scripts.import_excel_to_db import DB
        with pymysql.connect(**DB) as conn:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute('SELECT source_name, source_type, source_system, COUNT(*) total, '
                            'MIN(year) first_year, MAX(year) last_year, '
                            'SUM(year="2025") y2025, SUM(year="2026") y2026 '
                            'FROM papers GROUP BY source_name, source_type, source_system ORDER BY source_name')
                report['database_sources'] = cur.fetchall()
        print(json.dumps(report['database_sources'], ensure_ascii=False, default=str), flush=True)
    if args.browser:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from scripts.sweep_ieee_journals import make_driver
        from scripts.metadata_health import capture_failure, safe_text, verify_ieee_session
        driver = make_driver(True)
        try:
            target = ('https://ieeexplore.ieee.org/xpl/issues?punumber=4&isyear=2026'
                      if args.browser == 'journal' else
                      'https://ieeexplore.ieee.org/xpl/conhome/1000708/all-proceedings')
            driver.get('https://access.yonsei.ac.kr/link.n2s?url=' + target)
            wait = WebDriverWait(driver, 25)
            wait.until(EC.presence_of_element_located((By.ID, 'id'))).send_keys(os.getenv('YONSEI_ID'))
            driver.find_element(By.ID, 'password').send_keys(os.getenv('YONSEI_PW'))
            driver.find_element(By.XPATH, "//input[@type='submit' and @value='로그인']").click()
            selector = 'li h2 a' if args.browser == 'conference' else '.issue-details-past-tabs'
            try:
                verify_ieee_session(driver, wait)
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
            except Exception as exc:
                report['browser_exception'] = type(exc).__name__
            evidence_path = capture_failure(driver, ROOT / 'logs' / 'metadata_browser',
                                            args.browser, 'probe', RuntimeError(report.get('browser_exception', 'probe')))
            report['browser'] = json.loads(Path(evidence_path).read_text(encoding='utf-8'))
            report['browser']['body_excerpt'] = safe_text(driver.find_element(By.TAG_NAME, 'body').text)
            print(json.dumps(report['browser'], ensure_ascii=False), flush=True)
        finally:
            driver.quit()
    path = ROOT / 'logs' / f'metadata_probe_{datetime.now():%Y%m%d_%H%M%S}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(path)


if __name__ == '__main__':
    main()
