"""One-time additive migration; new accounts await a locally chosen password."""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import URL, create_engine, inspect, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def connect_engine():
    values = dotenv_values(ROOT / '.env')
    engine = create_engine(URL.create('mysql+pymysql', username=values['DB_USER'],
        password=values['DB_PASSWORD'], host=values.get('DB_HOST', '127.0.0.1'),
        port=int(values.get('DB_PORT', 3306)), database=values['DB_NAME']), pool_pre_ping=True)
    return engine, values


def digest(values):
    return hashlib.sha256(json.dumps(sorted(values)).encode()).hexdigest()


def migrate(engine, values, new_user, source_user, apply=False, admin_engine=None):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', new_user):
        raise ValueError('Use 1-64 ASCII letters, numbers, dots, underscores, or hyphens.')
    if new_user in {values.get('AUTH_USERNAME'), values.get('ADMIN_USERNAME')}:
        raise ValueError('The new account already exists in the legacy configuration.')
    with engine.connect() as conn:
        active = conn.execute(text("SELECT COUNT(*) FROM ai_jobs WHERE status IN ('queued','running')")).scalar()
        papers = conn.execute(text('SELECT COUNT(*) FROM papers')).scalar()
        favorites = list(conn.execute(text('SELECT id FROM papers WHERE is_favorite=1')).scalars())
        existing = inspect(conn).has_table('accounts')
        report = dict(papers=papers, legacy_favorites=len(favorites), favorites_sha256=digest(favorites),
                      legacy_admin=values.get('ADMIN_USERNAME'), legacy_viewer=values.get('AUTH_USERNAME'),
                      active_ai_jobs=active, accounts_table_exists=existing, apply=apply)
        if not apply:
            return report
        if active:
            raise RuntimeError('Wait for active AI work before migrating.')
        if source_user != values.get('ADMIN_USERNAME') and not existing:
            raise ValueError('Initial source must be the configured administrator.')
    ddl = (ROOT / 'scripts/migrations/013_accounts.sql').read_text(encoding='utf-8')
    with engine.begin() as conn:
        for statement in ddl.split(';'):
            if statement.strip():
                conn.execute(text(statement))
        columns = {column['name'] for column in inspect(conn).get_columns('ai_jobs')}
        if 'account_id' not in columns:
            conn.execute(text('ALTER TABLE ai_jobs ADD COLUMN account_id BIGINT UNSIGNED NULL,'
                ' ADD KEY idx_ai_job_account (account_id,created_at),'
                ' ADD CONSTRAINT fk_ai_job_account FOREIGN KEY (account_id) REFERENCES accounts(id)'))
    with engine.begin() as conn:
        # Seed each old login once. Re-running never resets passwords or private state.
        for name, hashed, role, legacy in [
            (values['ADMIN_USERNAME'], values['ADMIN_PASSWORD_HASH'], 'admin', 1),
            (values['AUTH_USERNAME'], values['AUTH_PASSWORD_HASH'], 'viewer', 0),
        ]:
            account_id = conn.execute(text('SELECT id FROM accounts WHERE username=:name'), {'name': name}).scalar()
            if account_id is None:
                result = conn.execute(text('INSERT INTO accounts(username,password_hash,role,is_active,legacy_owner) '
                    'VALUES (:name,:hashed,:role,1,:legacy)'),
                    dict(name=name, hashed=hashed, role=role, legacy=legacy))
                account_id = result.lastrowid
                conn.execute(text('INSERT INTO account_favorites(account_id,paper_id) '
                    'SELECT :account,id FROM papers WHERE is_favorite=1'), {'account': account_id})
                conn.execute(text('INSERT INTO account_recommendation_feedback(account_id,article_number,action,updated_at) '
                    'SELECT :account,article_number,action,updated_at FROM recommendation_feedback'), {'account': account_id})
            conn.execute(text('UPDATE ai_jobs SET account_id=:id WHERE owner=:name AND account_id IS NULL'),
                         {'id': account_id, 'name': name})
        source_id = conn.execute(text('SELECT id FROM accounts WHERE username=:name'), {'name': source_user}).scalar()
        if source_id is None:
            raise ValueError('Source account does not exist.')
        new_id = conn.execute(text('SELECT id FROM accounts WHERE username=:name'), {'name': new_user}).scalar()
        created = new_id is None
        if created:
            new_id = conn.execute(text("INSERT INTO accounts(username,role) VALUES (:name,'member')"),
                                  {'name': new_user}).lastrowid
            conn.execute(text('INSERT INTO account_favorites(account_id,paper_id) '
                'SELECT :new,paper_id FROM account_favorites WHERE account_id=:source'),
                {'new': new_id, 'source': source_id})
        copied = list(conn.execute(text('SELECT paper_id FROM account_favorites WHERE account_id=:id'),
                                   {'id': new_id}).scalars())
        source = list(conn.execute(text('SELECT paper_id FROM account_favorites WHERE account_id=:id'),
                                   {'id': source_id}).scalars())
        if created and set(copied) != set(source):
            raise RuntimeError('Favorite copy verification failed.')
        report.update(new_account_id=new_id, source_account_id=source_id, created=created,
                      copied_favorites=len(copied), copied_sha256=digest(copied),
                      source_sha256=digest(source), password_configured=False)
    # Legacy maintenance scripts still write papers.is_favorite. Propagate only
    # to the designated original owner's private rows; never to other accounts.
    with (admin_engine or engine).begin() as conn:
        triggers = set(conn.execute(text('SELECT TRIGGER_NAME FROM information_schema.TRIGGERS '
            'WHERE TRIGGER_SCHEMA=DATABASE()')).scalars())
        for event, name, condition in [
            ('INSERT', 'paper_account_favorite_insert', 'NEW.is_favorite=1'),
            ('UPDATE', 'paper_account_favorite_update', 'NOT (NEW.is_favorite <=> OLD.is_favorite)'),
        ]:
            if name in triggers:
                continue
            conn.execute(text(f'''CREATE TRIGGER {name} AFTER {event} ON papers FOR EACH ROW
                BEGIN
                    IF {condition} THEN
                        IF NEW.is_favorite=1 THEN
                            INSERT IGNORE INTO account_favorites(account_id,paper_id)
                                SELECT id,NEW.id FROM accounts WHERE legacy_owner=1;
                        ELSE
                            DELETE af FROM account_favorites af JOIN accounts a ON a.id=af.account_id
                                WHERE a.legacy_owner=1 AND af.paper_id=NEW.id;
                        END IF;
                    END IF;
                END'''))
    with engine.connect() as conn:
        after = list(conn.execute(text('SELECT id FROM papers WHERE is_favorite=1')).scalars())
        if set(after) != set(favorites) or conn.execute(text('SELECT COUNT(*) FROM papers')).scalar() != papers:
            raise RuntimeError('Paper state changed during migration; inspect concurrent activity.')
    report['legacy_papers_preserved'] = True
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--new-user', required=True)
    parser.add_argument('--copy-from', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--admin-defaults', type=Path,
                        help='Local MySQL client configuration for creating compatibility triggers only.')
    args = parser.parse_args()
    db, config = connect_engine()
    admin_db = None
    if args.admin_defaults:
        import pymysql
        admin_db = create_engine('mysql+pymysql://', creator=lambda: pymysql.connect(
            read_default_file=str(args.admin_defaults), database=config['DB_NAME'], charset='utf8mb4'))
    print(json.dumps(migrate(db, config, args.new_user, args.copy_from, args.apply, admin_db),
                     ensure_ascii=False, indent=2))
