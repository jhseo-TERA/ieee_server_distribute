"""Cached public metadata requests with a persistent, shared IEEE allowance.

Only fixed provider hosts are contacted. Keys and failing response bodies never
enter the cache or errors. Reservations count attempts, including failed calls.
"""
from collections import Counter
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import urlsplit

import requests


DAY = 86400
ROOT = Path(__file__).resolve().parents[1]


class SourceUnavailable(Exception):
    def __init__(self, reason, status=503):
        super().__init__(reason)
        self.reason, self.status = reason, status


def setting(name, default, minimum=0, maximum=100000):
    try:
        return max(minimum, min(maximum, int(os.getenv(name, default))))
    except (TypeError, ValueError):
        return default


class CitationHTTP:
    def __init__(self, path=None, session=None, clock=time.time, sleep=time.sleep,
                 ieee_limit=None, interval=None):
        self.path = Path(path or ROOT / 'py_01_data/connection/provider-state.sqlite3')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.session, self.clock, self.sleep = session, clock, sleep
        self.local = threading.local()
        self.ieee_limit = setting('IEEE_API_DAILY_LIMIT', 50) if ieee_limit is None else ieee_limit
        self.interval = setting('IEEE_API_MIN_INTERVAL_SECONDS', 2, 1, 60) if interval is None else interval
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS responses (
                    key TEXT PRIMARY KEY, payload TEXT, error TEXT, status INTEGER,
                    expires REAL NOT NULL DEFAULT 0, lease REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS attempts (key TEXT, stamp REAL);
                CREATE INDEX IF NOT EXISTS attempts_key_stamp ON attempts(key,stamp);
                CREATE TABLE IF NOT EXISTS gates (
                    key TEXT PRIMARY KEY, next_call REAL DEFAULT 0, blocked REAL DEFAULT 0);
            ''')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute('PRAGMA busy_timeout=5000')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def key_id(key):
        return sha256(key.encode()).hexdigest()

    def _reserve(self, key):
        now, identity = self.clock(), self.key_id(key)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM attempts WHERE stamp<?', (now - DAY,))
            gate = db.execute('SELECT next_call,blocked FROM gates WHERE key=?', (identity,)).fetchone() or (0, 0)
            count = db.execute('SELECT COUNT(*) FROM attempts WHERE key=? AND stamp>?', (identity, now-DAY)).fetchone()[0]
            if gate[1] > now:
                raise SourceUnavailable('ieee_cooldown', 429)
            if count >= self.ieee_limit:
                raise SourceUnavailable('ieee_local_budget', 429)
            slot = max(now, gate[0])
            if slot - now > 5:
                raise SourceUnavailable('ieee_pacing', 429)
            db.execute('INSERT OR REPLACE INTO gates VALUES (?,?,?)', (identity, slot+self.interval, gate[1]))
            db.execute('INSERT INTO attempts VALUES (?,?)', (identity, slot))
        if slot > now:
            self.sleep(slot - now)

    def _block(self, key, seconds):
        with self._db() as db:
            identity = self.key_id(key)
            db.execute('INSERT OR IGNORE INTO gates(key) VALUES (?)', (identity,))
            db.execute('UPDATE gates SET blocked=MAX(blocked,?) WHERE key=?', (self.clock()+seconds, identity))

    def budget(self, key):
        now = self.clock()
        with self._db() as db:
            used = db.execute('SELECT COUNT(*) FROM attempts WHERE key=? AND stamp>?', (self.key_id(key), now-DAY)).fetchone()[0]
        return {'limit': self.ieee_limit, 'used': used, 'remaining': max(0, self.ieee_limit-used), 'window_hours': 24}

    def counts(self):
        return dict(getattr(self.local, 'counts', {}))

    def get(self, provider, url, params=None, key='', ttl=30*DAY):
        try:
            return self._get(provider,url,params,key,ttl)
        except sqlite3.Error:
            raise SourceUnavailable('provider_state_unavailable') from None

    def _get(self, provider, url, params=None, key='', ttl=30*DAY):
        host = {'crossref': 'api.crossref.org', 'ieee': 'ieeexploreapi.ieee.org'}
        parsed = urlsplit(url)
        if provider not in host or parsed.scheme != 'https' or parsed.netloc != host[provider] or parsed.query:
            raise SourceUnavailable('invalid_provider')
        if provider == 'ieee' and not key:
            raise SourceUnavailable('ieee_not_configured')
        params = dict(params or {})
        # A caller may pass apikey; remove it before deriving/storing cache keys.
        params.pop('apikey', None)
        identity = sha256(json.dumps([provider, url, params, self.key_id(key) if key else ''], sort_keys=True).encode()).hexdigest()
        now = self.clock()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload,error,status,expires,lease FROM responses WHERE key=?', (identity,)).fetchone()
            if row and row[3] > now:
                if row[1]:
                    raise SourceUnavailable(row[1], row[2])
                return json.loads(row[0])
            if row and row[4] > now:
                raise SourceUnavailable('provider_request_pending')
            db.execute('INSERT OR REPLACE INTO responses(key,lease) VALUES (?,?)', (identity, now+45))
        payload, error, status, cache_for = None, None, 200, ttl
        try:
            if provider == 'ieee':
                self._reserve(key)
                params['apikey'] = key
            if self.session is None:
                if not hasattr(self.local, 'session'):
                    self.local.session = requests.Session()
                client = self.local.session
            else:
                client = self.session
            if not hasattr(self.local, 'counts'):
                self.local.counts = Counter()
            self.local.counts[provider] += 1
            response = client.get(url, params=params, headers={'Accept': 'application/json',
                'User-Agent': 'Paper-Archive/1.0 (citation-map)'}, timeout=(3.05, 10), allow_redirects=False)
            status = response.status_code
            if status != 200:
                if provider == 'ieee' and status in (401, 403, 429):
                    try:
                        delay = float(response.headers.get('Retry-After', 3600))
                    except (TypeError, ValueError):
                        delay = 3600
                    self._block(key, max(3600, min(DAY, delay)))
                raise SourceUnavailable(provider + '_http_' + str(status), status)
            payload = response.json()
            serialized = json.dumps(payload)
            if key and key in serialized:
                raise SourceUnavailable(provider + '_sensitive_response')
            if not isinstance(payload, dict) or len(serialized) > 8_000_000:
                raise SourceUnavailable(provider + '_invalid_response')
        except SourceUnavailable as exc:
            error, status = exc.reason, exc.status
            cache_for = 3600 if provider == 'ieee' else 300
        except (requests.RequestException, ValueError, TypeError) as exc:
            error, status, cache_for = provider + '_' + type(exc).__name__.lower(), 503, 3600 if provider == 'ieee' else 300
        with self._db() as db:
            db.execute('INSERT OR REPLACE INTO responses VALUES (?,?,?,?,?,0)',
                       (identity, json.dumps(payload) if not error else None, error, status, self.clock()+cache_for))
            if db.execute('SELECT COUNT(*) FROM responses').fetchone()[0] > 4096:
                db.execute('DELETE FROM responses WHERE key IN (SELECT key FROM responses ORDER BY expires LIMIT 256)')
        if error:
            raise SourceUnavailable(error, status)
        return payload
