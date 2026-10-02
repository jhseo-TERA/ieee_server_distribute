"""Failure evidence and explicit exit semantics for legacy browser collectors."""
import json
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]


def verify_ieee_session(driver, wait):
    try:
        wait.until(lambda d: 'ieeexplore' in urlsplit(d.current_url).netloc
                   and not d.find_elements('css selector', '#password'))
    except Exception as exc:
        raise RuntimeError('IEEE login not completed; browser is still on a login/proxy page') from exc


def safe_text(value):
    text = str(value)
    for key in ('YONSEI_ID', 'YONSEI_PW', 'DB_PASSWORD'):
        secret = os.getenv(key)
        if secret:
            text = text.replace(secret, '[redacted]')
    return text[:2000]


def capture_failure(driver, directory, source, stage, error):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%H%M%S_%f')
    stem = re.sub(r'[^\w-]', '_', f'{source}_{stage}_{stamp}')[:150]
    evidence = {'source': source, 'stage': stage, 'exception': type(error).__name__,
                'message': safe_text(error)}
    if driver is not None:
        try:
            url = urlsplit(driver.current_url)
            # Authentication tokens can occur in URL query strings.
            evidence.update(url=urlunsplit((url.scheme, url.netloc, url.path, '', '')),
                            title=safe_text(driver.title))
            evidence['selectors'] = {selector: len(driver.find_elements('css selector', selector))
                                     for selector in ('#id', '#password', 'li h2 a',
                                                      '.issue-details-past-tabs', '.List-results-items')}
            # Do not persist typed credentials in diagnostic screenshots.
            driver.execute_script("document.querySelectorAll('input').forEach(e => e.value = '')")
            screenshot = directory / f'{stem}.png'
            if driver.save_screenshot(str(screenshot)):
                evidence['screenshot'] = str(screenshot)
        except Exception as capture_error:
            evidence['capture_error'] = type(capture_error).__name__
    path = directory / f'{stem}.json'
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    return str(path)


class BrowserRun:
    def __init__(self, label, sources):
        self.sources = list(sources)
        self.failures = []
        self.directory = ROOT / 'logs' / 'metadata_browser' / f'{label}_{datetime.now():%Y%m%d_%H%M%S_%f}'

    def fail(self, driver, source, stage, error):
        path = capture_failure(driver, self.directory, source, stage, error)
        self.failures.append({'source': source, 'stage': stage, 'evidence': path})
        print(f'[실패] {source} / {stage}: {type(error).__name__}; evidence={path}')

    def finish(self, rows):
        counts = Counter(row.get('Journal') or row.get('Conference') for row in rows)
        failed = {item['source'] for item in self.failures if item['source'] in self.sources}
        if any(item['source'] not in self.sources for item in self.failures):
            failed.update(self.sources)
        failed.update(name for name in self.sources if counts[name] == 0)
        summary = {'counts': dict(counts), 'failed_sources': sorted(failed),
                   'success_count': len(set(self.sources) - failed), 'failure_count': len(failed),
                   'failures': self.failures, 'status': 'failed' if failed or not rows else 'completed'}
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        print('[수집 요약] ' + json.dumps(summary, ensure_ascii=False))
        if summary['status'] == 'failed':
            raise RuntimeError('Incomplete browser sweep; see summary.json')
        return summary
