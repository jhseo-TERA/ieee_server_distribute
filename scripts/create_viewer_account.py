"""Create an additional viewer with a live favorites reference and its own login.

Default is a read-only preflight. Back up MySQL before --apply. No existing
account is changed, and no plaintext password is stored or passed on the CLI.
"""
import argparse
import getpass
import json
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import inspect, text
from werkzeug.security import generate_password_hash
from paper_server.accounts import validate_password
from scripts.migrate_accounts import connect_engine


def create_viewer(engine, username, source_username, password=None, *, apply=False,
                  must_change_password=False):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', username):
        raise ValueError('Use 1-64 ASCII letters, numbers, dots, underscores, or hyphens.')
    with engine.connect() as conn:
        if conn.execute(text('SELECT id FROM accounts WHERE username=:name'),
                        {'name': username}).first():
            raise ValueError('The account already exists; no changes were made.')
        source = conn.execute(text('SELECT id,role,is_active FROM accounts WHERE username=:name'),
                              {'name': source_username}).mappings().first()
        if not source or not source['is_active'] or source['role'] not in {'admin', 'member'}:
            raise ValueError('The source must be an active admin or member account.')
        count = conn.execute(text('SELECT COUNT(*) FROM account_favorites WHERE account_id=:id'),
                             {'id': source['id']}).scalar_one()
        columns = {column['name'] for column in inspect(conn).get_columns('accounts')}
    report = dict(username=username, role='viewer', favorites_source=source_username,
                  favorites=count, apply=apply,
                  migration_required='favorites_source_account_id' not in columns)
    if not apply:
        return report
    if password is not None:
        validate_password(password, username)
    hashed = generate_password_hash(password) if password is not None else None
    active = password is not None
    if report['migration_required']:
        ddl = (ROOT / 'scripts/migrations/014_viewer_favorites_source.sql').read_text(encoding='utf-8')
        with engine.begin() as conn:
            conn.execute(text(ddl))
    with engine.begin() as conn:
        source = conn.execute(text('SELECT id,role,is_active FROM accounts WHERE username=:name FOR UPDATE'),
                              {'name': source_username}).mappings().one()
        if not source['is_active'] or source['role'] not in {'admin', 'member'}:
            raise ValueError('The source account changed; no viewer was created.')
        result = conn.execute(text('INSERT INTO accounts '
            '(username,password_hash,role,is_active,must_change_password,favorites_source_account_id) '
            "VALUES (:name,:hashed,'viewer',:active,:change,:source)"),
            dict(name=username, hashed=hashed, active=int(active), change=int(must_change_password), source=source['id']))
        report.update(account_id=result.lastrowid, active=active,
                      must_change_password=must_change_password)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--username', required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--apply', action='store_true')
    password_options = parser.add_mutually_exclusive_group()
    password_options.add_argument('--generate-password', action='store_true',
                        help='Display a random temporary password once; require a change at first login.')
    password_options.add_argument('--defer-password', action='store_true',
                        help='Create an inactive account; activate later with setup_account_auth.py.')
    args = parser.parse_args()
    engine, _ = connect_engine()
    # Validate before prompting for a password or changing the schema.
    report = create_viewer(engine, args.username, args.source)
    if not args.apply:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    password = (None if args.defer_password else secrets.token_urlsafe(18)
                if args.generate_password else getpass.getpass('New password: '))
    if not args.generate_password and not args.defer_password and password != getpass.getpass('Confirm password: '):
        raise SystemExit('Passwords do not match.')
    report = create_viewer(engine, args.username, args.source, password, apply=True,
                           must_change_password=args.generate_password)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.generate_password:
        print('Temporary password (change at first login):', password)


if __name__ == '__main__':
    main()
