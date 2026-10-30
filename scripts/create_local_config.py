"""Create first-install .env credentials interactively; never overwrite a file."""
import getpass
import json
from pathlib import Path
import re
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_server.accounts import validate_password
from werkzeug.security import generate_password_hash


def create_config(path, *, db_user, db_password, admin_password, viewer_password,
                  db_host='127.0.0.1', db_port=3306):
    validate_password(admin_password, 'admin')
    validate_password(viewer_password, 'viewer')
    if not db_user or not db_password or not db_host or not 1 <= int(db_port) <= 65535:
        raise ValueError('A database host, user, password and valid port are required.')
    values = dict(DB_HOST=db_host, DB_PORT=str(db_port), DB_USER=db_user,
                  DB_PASSWORD=db_password, DB_NAME='ieee_repo',
                  AUTH_USERNAME='viewer', AUTH_PASSWORD_HASH=generate_password_hash(viewer_password),
                  ADMIN_USERNAME='admin', ADMIN_PASSWORD_HASH=generate_password_hash(admin_password),
                  APP_SECRET_KEY=secrets.token_urlsafe(32), ACCOUNT_AUTH_ENABLED='1')
    content = (ROOT / '.env.example').read_text(encoding='utf-8')
    for key, value in values.items():
        if '\n' in value or '\r' in value:
            raise ValueError('Configuration values cannot contain line breaks.')
        line = key + '=' + json.dumps(value, ensure_ascii=False)
        pattern = re.compile(r'^' + re.escape(key) + r'=.*$', re.M)
        if pattern.search(content):
            content = pattern.sub(lambda _: line, content)
        else:
            content += '\n' + line + '\n'
    with Path(path).open('x', encoding='utf-8') as handle:
        handle.write(content)


def main():
    target = ROOT / '.env'
    if target.exists():
        raise SystemExit('.env already exists; edit it locally instead of overwriting it.')
    host = input('MySQL host [127.0.0.1]: ').strip() or '127.0.0.1'
    port = int(input('MySQL port [3306]: ').strip() or '3306')
    user = input('MySQL user [ieee_app]: ').strip() or 'ieee_app'
    db_password = getpass.getpass('MySQL password: ')
    passwords = {}
    for username in ('admin', 'viewer'):
        password = getpass.getpass(f'{username} password (8+ characters, without the username): ')
        if password != getpass.getpass('Confirm password: '):
            raise SystemExit('Passwords do not match; no file was created.')
        passwords[username] = password
    try:
        create_config(target, db_user=user, db_password=db_password, db_host=host,
                      db_port=port, admin_password=passwords['admin'], viewer_password=passwords['viewer'])
    except ValueError as error:
        raise SystemExit(str(error)) from None
    print('Created .env with password hashes and a new session key. No database changes were made.')


if __name__ == '__main__':
    main()
