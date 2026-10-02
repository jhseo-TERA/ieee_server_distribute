# -*- coding: utf-8 -*-
"""Legacy unrestricted downloader entrypoint.

The former implementation allowed bulk, non-favorite downloads and is kept
only as a compatibility filename.  All PDF acquisition must go through the
daily routine, which enforces the shared 30-item daily budget.
"""


def main() -> int:
    print(
        "[실패] 무제한 백업 다운로더는 비활성화되었습니다. "
        "scripts\\run_pdf_download_daily.bat 또는 "
        "scripts\\run_pdf_download_routine.py를 사용하세요."
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
