"""Root-only login presence, without exposing cookies or collecting browsing history."""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import threading
import time

from flask import Blueprint, abort, g, jsonify, render_template, request, session
from sqlalchemy import BigInteger, Column, DateTime, Index, Integer, MetaData, String, Table, select, text
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from paper_server.accounts import current_account_id


ROOT_USERNAME = 'admin'
ACTIVE_SECONDS = 300
TOUCH_SECONDS = 30
metadata = MetaData()
timestamp = DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql')
login_sessions = Table('account_login_sessions', metadata,
    Column('session_key', String(64), primary_key=True),
    Column('account_id', BigInteger().with_variant(mysql.BIGINT(unsigned=True), 'mysql'), nullable=False),
    Column('session_version', Integer, nullable=False),
    Column('first_seen_at', timestamp, nullable=False),
    Column('login_at', timestamp),
    Column('last_seen_at', timestamp, nullable=False),
    Column('expires_at', timestamp, nullable=False),
    Column('ended_at', timestamp),
    Column('end_reason', String(16)),
    Index('idx_account_login_seen', 'account_id', 'last_seen_at'),
    Index('idx_account_login_expires', 'expires_at'),
    mysql_charset='utf8mb4', mysql_collate='utf8mb4_unicode_ci')
_recent = {}
_recent_lock = threading.Lock()


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(value):
    if not value:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc).isoformat()


class ActivityStore:
    def __init__(self, engine):
        self.engine = engine

    def touch(self, key, account, version, lifetime, login=False, now=None):
        now = now or utcnow()
        values = dict(session_key=key, account_id=account, session_version=version,
            first_seen_at=now, login_at=now if login else None, last_seen_at=now,
            expires_at=now + lifetime)
        try:
            with self.engine.begin() as conn:
                exists = conn.execute(select(login_sessions.c.session_key).where(
                    login_sessions.c.session_key == key)).first()
                if not exists:
                    conn.execute(login_sessions.insert().values(**values))
                    return
                self._update(conn, key, account, version, now, now + lifetime)
        except IntegrityError:
            # Concurrent first requests from an existing cookie use the same key.
            with self.engine.begin() as conn:
                self._update(conn, key, account, version, now, now + lifetime)

    @staticmethod
    def _update(conn, key, account, version, now, expires):
        conn.execute(login_sessions.update().where(login_sessions.c.session_key == key,
            login_sessions.c.account_id == account, login_sessions.c.ended_at.is_(None)).values(
                session_version=version, last_seen_at=now, expires_at=expires))

    def logout(self, key, account, now=None):
        now = now or utcnow()
        with self.engine.begin() as conn:
            conn.execute(login_sessions.update().where(login_sessions.c.session_key == key,
                login_sessions.c.account_id == account, login_sessions.c.ended_at.is_(None)).values(
                    ended_at=now, end_reason='logout'))

    def overview(self, now=None):
        now = now or utcnow()
        active = '(s.ended_at IS NULL AND s.expires_at>:now AND s.session_version=a.session_version AND a.is_active=1)'
        params = dict(now=now.isoformat(' ', timespec='microseconds'),
                      since=(now - timedelta(seconds=ACTIVE_SECONDS)).isoformat(' ', timespec='microseconds'))
        with self.engine.connect() as conn:
            rows = conn.execute(text('SELECT a.id,a.username,a.role,a.is_active,'
                'MAX(s.login_at) last_login_at,MAX(s.last_seen_at) last_seen_at,'
                'COUNT(s.session_key) observed_sessions,'
                f'SUM(CASE WHEN {active} THEN 1 ELSE 0 END) active_sessions,'
                f'SUM(CASE WHEN {active} AND s.last_seen_at>=:since THEN 1 ELSE 0 END) online_sessions '
                'FROM accounts a LEFT JOIN account_login_sessions s ON s.account_id=a.id '
                'GROUP BY a.id,a.username,a.role,a.is_active ORDER BY a.id'), params).mappings().all()
            history = conn.execute(text('SELECT a.username,a.role,a.is_active,'
                'a.session_version current_version,s.session_version,s.first_seen_at,s.login_at,'
                's.last_seen_at,s.expires_at,s.ended_at,s.end_reason '
                'FROM account_login_sessions s JOIN accounts a ON a.id=s.account_id '
                'ORDER BY s.first_seen_at DESC,s.session_key LIMIT 40')).mappings().all()
        accounts = []
        for row in rows:
            item = dict(row)
            item['is_active'] = bool(item['is_active'])
            for field in ('observed_sessions', 'active_sessions', 'online_sessions'):
                item[field] = int(item[field] or 0)
            item['status'] = ('disabled' if not item['is_active'] else 'online' if item['online_sessions']
                else 'idle' if item['active_sessions'] else 'unknown' if not item['observed_sessions'] else 'offline')
            for field in ('last_login_at', 'last_seen_at'):
                item[field] = iso(item[field])
            accounts.append(item)
        recent = []
        for row in history:
            item = {key: row[key] for key in ('username', 'role', 'end_reason')}
            for field in ('first_seen_at','login_at','last_seen_at','expires_at','ended_at'):
                item[field] = iso(row[field])
            item['kind'] = 'login' if row['login_at'] else 'observed'
            item['status'] = ('logout' if row['ended_at'] else 'revoked'
                if not row['is_active'] or row['session_version'] != row['current_version'] else 'expired'
                if item['expires_at'] <= iso(now) else 'online'
                if item['last_seen_at'] >= iso(now - timedelta(seconds=ACTIVE_SECONDS)) else 'idle')
            recent.append(item)
        return dict(accounts=accounts, recent_sessions=recent, generated_at=iso(now),
                    active_window_seconds=ACTIVE_SECONDS, refresh_seconds=30)


def activity_key(app):
    # Stable for simultaneous requests from a cookie issued before this feature.
    seed = session.get('activity_session_id')
    if not seed:
        csrf = session.get('csrf_token')
        if not csrf:
            return None
        seed = hmac.new(str(app.secret_key).encode(),
            f'presence:{session.get("account_id")}:{csrf}'.encode(), hashlib.sha256).hexdigest()
        session['activity_session_id'] = seed
    return hashlib.sha256(seed.encode()).hexdigest()


def capture_logout(app):
    key = activity_key(app)
    if key and current_account_id.get() is not None:
        g.activity_logout = (key, current_account_id.get())


def install_account_activity(app, get_engine):
    bp = Blueprint('account_activity', __name__)

    @bp.route('/account/activity')
    def page():
        if session.get('username') != ROOT_USERNAME:
            abort(403)
        return render_template('account_activity.html', username=session['username'], csrf_token=session['csrf_token'])

    @bp.route('/api/account/activity')
    def overview():
        if session.get('username') != ROOT_USERNAME:
            return jsonify(ok=False, error='로그인 현황은 admin 계정만 조회할 수 있습니다.'), 403
        if not app.config.get('ACCOUNT_AUTH_ENABLED') or not app.config.get('ACCOUNT_ACTIVITY_ENABLED'):
            return jsonify(ok=False, error='계정별 로그인 현황 기록이 설정되지 않았습니다.'), 503
        try:
            return jsonify(ok=True, **ActivityStore(get_engine()).overview())
        except SQLAlchemyError:
            return jsonify(ok=False, error='로그인 현황 저장소에 연결할 수 없습니다.'), 503

    @bp.route('/api/account/heartbeat', methods=['POST'])
    def heartbeat():
        return jsonify(ok=True)

    @app.after_request
    def record_activity(response):
        if not app.config.get('ACCOUNT_ACTIVITY_ENABLED') or not app.config.get('ACCOUNT_AUTH_ENABLED'):
            return response
        ended = g.get('activity_logout')
        account = current_account_id.get()
        login = g.get('activity_login', False)
        if not ended and (not session.get('authenticated') or response.status_code >= 500
                or not (account is not None or login)):
            return response
        engine = get_engine()
        key = ended[0] if ended else activity_key(app)
        if not key:
            return response
        cache_key = (id(engine), key, session.get('session_version'))
        moment = time.monotonic()
        with _recent_lock:
            if not ended and not login and moment - _recent.get(cache_key, -TOUCH_SECONDS) < TOUCH_SECONDS:
                return response
        try:
            store = ActivityStore(engine)
            if ended:
                store.logout(*ended)
            else:
                store.touch(key, session['account_id'], session['session_version'], app.permanent_session_lifetime, login=login)
            with _recent_lock:
                if len(_recent) >= 4096:
                    _recent.clear()
                _recent[cache_key] = moment
        except SQLAlchemyError:
            # Presence must not break authentication or saving a review.
            app.logger.warning('Account activity write failed; check the activity migration.')
        return response

    app.register_blueprint(bp)
