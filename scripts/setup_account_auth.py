"""Set an existing account's password locally without exposing plaintext."""
import argparse
import getpass
import sys

from migrate_accounts import connect_engine, ROOT
sys.path.insert(0, str(ROOT))
from account_store import AccountStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--username', required=True)
    args = parser.parse_args()
    password = getpass.getpass(f'{args.username} 새 비밀번호 (8자 이상): ')
    confirmation = getpass.getpass('비밀번호 확인: ')
    if password != confirmation:
        raise SystemExit('두 비밀번호가 일치하지 않습니다. 다시 실행해 주세요.')
    engine, _ = connect_engine()
    try:
        AccountStore(engine).set_password(args.username, password)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    print('비밀번호가 저장되었고 계정이 활성화되었습니다. 서버 재시작 없이 로그인할 수 있습니다.')


if __name__ == '__main__':
    main()
