"""Account identities and private state; paper metadata/PDFs remain shared.

The context is set only by authenticated server code, never request parameters.
Unscoped CLI maintenance keeps the legacy administrator's paper flags.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from sqlalchemy import bindparam, text
from werkzeug.security import check_password_hash, generate_password_hash

current_account_id = ContextVar('paper_account_id', default=None)
_DUMMY_HASH = generate_password_hash('invalid-login-timing-padding')


@contextmanager
def account_scope(account_id):
    if account_id is not None and (type(account_id) is not int or account_id <= 0):
        raise ValueError('invalid account ID')
    token = current_account_id.set(account_id)
    try:
        yield
    finally:
        current_account_id.reset(token)


def favorite_sql(alias='papers'):
    """SQL expression using a validated server-owned integer and fixed alias.

    No user input is interpolated. A literal allows this expression to be reused
    by the existing bounded SQL builders, including nested survey queries.
    """
    if alias not in {'papers', 'p'}:
        raise ValueError('invalid paper alias')
    account_id = current_account_id.get()
    if account_id is None:
        return f'{alias}.is_favorite'
    if type(account_id) is not int or account_id <= 0:
        raise ValueError('invalid account ID')
    return (f'EXISTS (SELECT 1 FROM account_favorites af WHERE af.account_id={account_id} '
            f'AND af.paper_id={alias}.id)')


def validate_password(password, username):
    if not isinstance(password, str) or not 8 <= len(password) <= 256:
        raise ValueError('비밀번호는 8~256자로 지정하세요.')
    if username.casefold() in password.casefold():
        raise ValueError('비밀번호에 계정명을 포함할 수 없습니다.')


class AccountStore:
    def __init__(self, engine):
        self.engine = engine

    def get(self, account_id):
        if type(account_id) is not int or account_id <= 0:
            return None
        with self.engine.connect() as conn:
            row = conn.execute(text('SELECT * FROM accounts WHERE id=:id'),
                               {'id': account_id}).mappings().first()
        return dict(row) if row else None

    def authenticate(self, username, password):
        with self.engine.connect() as conn:
            row = conn.execute(text('SELECT * FROM accounts WHERE username=:name'),
                               {'name': username}).mappings().first()
        hashed = row['password_hash'] if row and row['is_active'] else _DUMMY_HASH
        valid = check_password_hash(hashed or _DUMMY_HASH, password)
        return dict(row) if row and row['is_active'] and valid else None

    def set_password(self, username, password):
        validate_password(password, username)
        with self.engine.begin() as conn:
            result = conn.execute(text('UPDATE accounts SET password_hash=:hashed,is_active=1,'
                'must_change_password=0,session_version=session_version+1 WHERE username=:name'),
                {'name': username, 'hashed': generate_password_hash(password)})
            if result.rowcount != 1:
                raise ValueError('계정을 찾을 수 없습니다.')

    def change_password(self, account_id, previous, password):
        with self.engine.begin() as conn:
            row = conn.execute(text('SELECT * FROM accounts WHERE id=:id FOR UPDATE'),
                               {'id': account_id}).mappings().first()
            if not row or not row['is_active'] or not check_password_hash(row['password_hash'], previous):
                raise ValueError('현재 비밀번호가 올바르지 않습니다.')
            validate_password(password, row['username'])
            conn.execute(text('UPDATE accounts SET password_hash=:hashed,must_change_password=0,'
                'session_version=session_version+1 WHERE id=:id'),
                {'id': account_id, 'hashed': generate_password_hash(password)})
            return row['session_version'] + 1

    def set_favorites(self, account_id, articles, value=None):
        """Serialize a single account's edits and preserve other accounts/metadata."""
        with self.engine.begin() as conn:
            account = conn.execute(text('SELECT id,legacy_owner,role,is_active FROM accounts '
                'WHERE id=:id FOR UPDATE'), {'id': account_id}).mappings().first()
            if not account or not account['is_active'] or account['role'] not in {'admin', 'member'}:
                raise PermissionError('personal settings permission required')
            stmt = text('SELECT id,article_number,citation_count FROM papers '
                'WHERE article_number IN :articles ORDER BY id').bindparams(bindparam('articles', expanding=True))
            rows = list(conn.execute(stmt, {'articles': articles}).mappings())
            if len(rows) != len(articles):
                raise KeyError('paper')
            selected = set(conn.execute(text('SELECT paper_id FROM account_favorites '
                'WHERE account_id=:id'), {'id': account_id}).scalars())
            delta = changed = 0
            final = None
            for row in rows:
                previous = row['id'] in selected
                final = (not previous) if value is None else bool(value)
                if previous == final:
                    continue
                params = {'account': account_id, 'paper': row['id']}
                if final:
                    conn.execute(text('INSERT INTO account_favorites(account_id,paper_id) '
                        'VALUES (:account,:paper)'), params)
                else:
                    conn.execute(text('DELETE FROM account_favorites '
                        'WHERE account_id=:account AND paper_id=:paper'), params)
                # Existing download/Zotero jobs remain bound to the original owner.
                if account['legacy_owner']:
                    conn.execute(text('UPDATE papers SET is_favorite=:value WHERE id=:paper'),
                                 {'value': int(final), 'paper': row['id']})
                delta += int(final) - int(previous)
                changed += 1
        return dict(changed_count=changed, favorite_delta=delta, is_favorite=int(final),
                    citation_count=rows[0]['citation_count'] if len(rows) == 1 else None)
