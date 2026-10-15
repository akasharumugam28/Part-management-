"""Login rate-limit counters stored in PostgreSQL.

Flask-Limiter's default in-memory storage is per process. On Vercel every
serverless instance is a separate process, so an attacker could spread login
attempts across instances. This storage keeps the counters in the shared
database instead (table ``rate_limits``, see db/schema_postgres.sql).

Selected automatically when DATABASE_URL is set and RATELIMIT_STORAGE_URI is
not (a redis:// URI still takes precedence if you configure one).
"""
import random
import time
from contextlib import contextmanager

import psycopg
from flask import has_app_context
from limits.storage import Storage

import database

SCHEME = "pglimits"


class PostgresStorage(Storage):
    STORAGE_SCHEME = [SCHEME]

    def __init__(self, uri=None, wrap_exceptions=False, dsn=None, connect_timeout=10, **options):
        super().__init__(uri, wrap_exceptions=wrap_exceptions, **options)
        self.dsn = dsn
        self.connect_timeout = connect_timeout

    @property
    def base_exceptions(self):
        return psycopg.Error

    @contextmanager
    def _db(self):
        if has_app_context():
            yield database.get_db()  # reuse the request's connection
            return
        db = database.connect_postgres(self.dsn, self.connect_timeout)
        try:
            yield db
        finally:
            db.close()

    def incr(self, key, expiry, amount=1):
        now = time.time()
        with self._db() as db:
            if random.random() < 0.02:  # occasionally drop expired counters
                db.execute("DELETE FROM rate_limits WHERE expires_at <= ?", (now,))
            # One atomic statement: a new or expired window starts at `amount`,
            # a live one is incremented. Safe under concurrent requests.
            return db.execute(
                "INSERT INTO rate_limits (key, count, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET "
                "count = CASE WHEN rate_limits.expires_at <= ? THEN EXCLUDED.count "
                "ELSE rate_limits.count + EXCLUDED.count END, "
                "expires_at = CASE WHEN rate_limits.expires_at <= ? THEN EXCLUDED.expires_at "
                "ELSE rate_limits.expires_at END "
                "RETURNING count",
                (key, amount, now + expiry, now, now),
            ).fetchone()[0]

    def get(self, key):
        with self._db() as db:
            row = db.execute(
                "SELECT count FROM rate_limits WHERE key = ? AND expires_at > ?", (key, time.time())
            ).fetchone()
        return row[0] if row else 0

    def get_expiry(self, key):
        now = time.time()
        with self._db() as db:
            row = db.execute(
                "SELECT expires_at FROM rate_limits WHERE key = ? AND expires_at > ?", (key, now)
            ).fetchone()
        return row[0] if row else now

    def check(self):
        try:
            with self._db() as db:
                db.execute("SELECT 1 FROM rate_limits LIMIT 1")
            return True
        except psycopg.Error:
            return False

    def reset(self):
        with self._db() as db:
            return db.execute("DELETE FROM rate_limits").rowcount

    def clear(self, key):
        with self._db() as db:
            db.execute("DELETE FROM rate_limits WHERE key = ?", (key,))
