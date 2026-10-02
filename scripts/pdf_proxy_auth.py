"""Verified publisher login and redacted evidence for PDF acquisition."""
import os
from pathlib import Path
from urllib.parse import urlsplit

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import StaleElementReferenceException

try:
    from .metadata_health import capture_failure
except ImportError:
    from metadata_health import capture_failure

ROOT = Path(__file__).resolve().parents[1]
PUBLISHER_HOSTS = {
    "ieee": {"ieeexplore.ieee.org", "ieeexplore-ieee-org.access.yonsei.ac.kr",
             "ieeexplore-ieee-org-ssl.access.yonsei.ac.kr"},
    "nature": {"www.nature.com", "nature.com", "www-nature-com.access.yonsei.ac.kr",
               "www-nature-com-ssl.access.yonsei.ac.kr"},
}


class ProxyLoginError(RuntimeError):
    pass


def credential_status():
    return {key: bool(os.getenv(key, "").strip()) for key in ("YONSEI_ID", "YONSEI_PW")}


def require_credentials():
    missing = [key for key, present in credential_status().items() if not present]
    if missing:
        raise ProxyLoginError("Missing " + ", ".join(missing) + "; set these locally in .env")


def visible_elements(driver, selector):
    return [e for e in driver.find_elements(By.CSS_SELECTOR, selector)
            if e.is_displayed() and e.is_enabled()]


def publisher_ready(driver, provider):
    url = urlsplit(driver.current_url or "")
    title = str(driver.title or "").lower()
    if any(marker in title for marker in ("access denied", "unable to load page", "security check", "captcha")):
        return False
    return (url.scheme == "https" and url.hostname in PUBLISHER_HOSTS[provider]
            and not visible_elements(driver, "input[type=password]"))


def verify_publisher(driver, provider, timeout=25):
    WebDriverWait(driver, timeout, ignored_exceptions=(StaleElementReferenceException,)).until(
        lambda d: publisher_ready(d, provider))
    return urlsplit(driver.current_url).hostname


def authenticate(driver, provider, entry_url, timeout=25):
    """Reuse a valid session or submit one visible login form, then verify host."""
    stage = "proxy_entry"
    try:
        driver.get(entry_url)
        WebDriverWait(driver, timeout, ignored_exceptions=(StaleElementReferenceException,)).until(
            lambda d: publisher_ready(d, provider) or visible_elements(d, "input[type=password]")
        )
        if not publisher_ready(driver, provider):
            stage = "login_form"
            login_url = urlsplit(driver.current_url or "")
            if login_url.scheme != "https" or login_url.hostname not in {"library.yonsei.ac.kr", "access.yonsei.ac.kr"}:
                raise ProxyLoginError("Login form is not on the expected university host")
            require_credentials()
            passwords = visible_elements(driver, "input[type=password]")
            password = passwords[0]
            form = password.find_element(By.XPATH, "ancestor::form")
            usernames = visible_elements(form, "#id, input[name=id], input[autocomplete=username]")
            if not usernames:
                raise ProxyLoginError("Visible username input was not found")
            usernames[0].clear()
            usernames[0].send_keys(os.environ["YONSEI_ID"])
            password.clear()
            password.send_keys(os.environ["YONSEI_PW"])
            submits = visible_elements(form, "input[type=submit], button[type=submit]")
            if not submits:
                raise ProxyLoginError("Visible login submit control was not found")
            stage = "login_verification"
            submits[0].click()
        return verify_publisher(driver, provider, timeout)
    except Exception as exc:
        try:
            evidence = capture_failure(driver, ROOT / "logs/pdf_download_state/login_diagnostics",
                                       provider, stage, exc)
        except Exception:
            evidence = "unavailable"
        # Never print Selenium's exception/URL: it can contain request tokens.
        raise ProxyLoginError(f"{provider} {stage} failed; evidence={evidence}") from None


def bootstrap_failure(provider, error):
    message = str(error) if isinstance(error, ProxyLoginError) else type(error).__name__
    print(f"[실패] {provider} PDF 준비 단계: {message}")
    # No article request was sent; release its daily reservation.
    print("[완료] 성공 0 / 건너뜀 0 / 실패 0")
    return 2
