# -*- coding: utf-8 -*-
"""One-shot IEEE proxy access diagnostic; never retries or saves a PDF."""
import argparse
from urllib.parse import urlparse

import requests

from download_pdfs import looks_like_blocked, looks_like_pdf, selenium_login


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("article_number")
    args = parser.parse_args()

    driver = selenium_login(headless=False)
    try:
        host = urlparse(driver.current_url).netloc
        session = requests.Session()
        try:
            browser_ua = driver.execute_script("return navigator.userAgent")
        except Exception:
            browser_ua = "Mozilla/5.0"
        session.headers.update({"User-Agent": browser_ua, "Accept-Language": "ko,en;q=0.9"})
        for cookie in driver.get_cookies():
            try:
                session.cookies.set(
                    cookie["name"], cookie["value"],
                    domain=cookie.get("domain"), path=cookie.get("path", "/"),
                )
            except Exception:
                pass

        stamp = f"https://{host}/stamp/stamp.jsp?tp=&arnumber={args.article_number}"
        url = f"https://{host}/stampPDF/getPDF.jsp?tp=&arnumber={args.article_number}&ref="
        response = session.get(
            url,
            headers={"Referer": stamp, "Accept": "application/pdf,*/*"},
            timeout=90,
            allow_redirects=True,
        )
        print(f"proxy_host={host}")
        print(f"status={response.status_code}")
        print(f"content_type={response.headers.get('Content-Type', '')}")
        print(f"final_host={urlparse(response.url).netloc}")
        print(f"is_pdf={looks_like_pdf(response)}")
        print(f"is_blocked={looks_like_blocked(response)}")
        print(f"bytes={len(response.content)}")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
