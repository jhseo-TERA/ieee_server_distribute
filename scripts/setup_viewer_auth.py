"""Interactively change the existing Paper Server read-only password.

The account name is read from AUTH_USERNAME. The password is entered twice
without echo and only its Werkzeug hash is written to .env.
"""

from __future__ import annotations

import getpass
import os
import re
from pathlib import Path

from dotenv import dotenv_values
from werkzeug.security import generate_password_hash


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
MIN_PASSWORD_LENGTH = 8


def upsert_env_value(content: str, key: str, value: str) -> str:
    line = f"{key}={value}"
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(content):
        return pattern.sub(line, content, count=1)
    separator = "" if not content or content.endswith(("\n", "\r")) else os.linesep
    return f"{content}{separator}{line}{os.linesep}"


def main() -> None:
    if not ENV_PATH.exists():
        raise SystemExit(f"[오류] .env 파일이 없습니다: {ENV_PATH}")

    username = str(dotenv_values(ENV_PATH).get("AUTH_USERNAME") or "").strip()
    if not username:
        raise SystemExit("[오류] .env의 AUTH_USERNAME이 비어 있습니다.")

    password = getpass.getpass(f"{username} 읽기 전용 계정 새 비밀번호: ")
    confirmation = getpass.getpass("새 비밀번호 확인: ")
    if password != confirmation:
        raise SystemExit("[오류] 두 비밀번호가 일치하지 않습니다.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SystemExit(f"[오류] 비밀번호는 최소 {MIN_PASSWORD_LENGTH}자여야 합니다.")
    if username.casefold() in password.casefold():
        raise SystemExit("[오류] 비밀번호에 읽기 전용 계정 ID를 포함할 수 없습니다.")

    if os.getenv('ACCOUNT_AUTH_ENABLED', '1') == '1':
        from migrate_accounts import connect_engine
        from account_store import AccountStore
        engine, _ = connect_engine()
        AccountStore(engine).set_password(username, password)
        print('[완료] 읽기 전용 계정 비밀번호가 DB에 저장되었습니다. 바로 로그인할 수 있습니다.')
        return

    content = ENV_PATH.read_text(encoding="utf-8")
    content = upsert_env_value(
        content, "AUTH_PASSWORD_HASH", generate_password_hash(password)
    )
    ENV_PATH.write_text(content, encoding="utf-8")
    print(f"[완료] {username} 읽기 전용 계정 해시가 .env에 저장되었습니다.")
    print("Paper Server를 재시작하면 새 비밀번호가 활성화됩니다.")


if __name__ == "__main__":
    main()
