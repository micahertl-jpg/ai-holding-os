"""
db.py — database layer for the AI Holding Company OS.

Two real implementations, same interface (execute/query/query_one/audit/
tx/close):
  - `Database` (SQLite) — the original, stdlib-only implementation. Used
    by demo.py/demo_real_llm.py directly, and used by api.py as the
    default when no DATABASE_URL is set, so you can run the API locally
    with zero external accounts.
  - `PostgresDatabase` — real Postgres via psycopg2. Used by api.py when
    DATABASE_URL points at Postgres (e.g. a Supabase/Railway connection
    string). Requires `pip install psycopg2-binary` — see requirements.txt.

Every query in this codebase is written with `?` placeholders (SQLite
style). PostgresDatabase translates `?` -> `%s` before executing. This
works because none of our SQL strings ever contain a literal `?`
character outside of parameter position — if you add a query with a
literal `?` in a string, don't use this translation blindly.

Both implementations serialize every operation behind a single
threading.Lock. Necessary because api.py's HTTP requests run across
FastAPI's worker thread pool AND a background scheduler thread all
share one Database instance — sqlite3 connections (even with
check_same_thread=False) and psycopg2 connections are not safe for
concurrent use from multiple threads without external serialization.
This makes every call go one-at-a-time rather than truly concurrent,
which is a real throughput ceiling worth knowing about — fine for an
MVP's request volume, wrong for anything high-concurrency; a real
connection pool (one connection per thread/request) is the eventual
fix, not this lock.

get_database() is the one factory the rest of the app should call.
"""

import sqlite3
import os
import uuid
import json
import threading
from contextlib import contextmanager

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")
SCHEMA_POSTGRES_PATH = os.path.join(os.path.dirname(__file__), "schema_postgres.sql")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Database:
    """SQLite implementation. `check_same_thread=False` is needed
    because FastAPI's sync endpoints run in a worker thread pool, not
    the connection's originating thread."""

    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.Lock()
        with open(SCHEMA_PATH) as f:
            self.conn.executescript(f.read())
        self.conn.commit()

    @contextmanager
    def tx(self):
        with self._lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def execute(self, sql, params=()):
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def query(self, sql, params=()):
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql, params=()):
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def audit(self, actor: str, action: str, target_type: str = None,
              target_id: str = None, details: dict = None):
        """Every meaningful state change should call this. This is the
        system's audit trail — do not skip it to save a line of code."""
        self.execute(
            "INSERT INTO audit_log (actor, action, target_type, target_id, details) "
            "VALUES (?, ?, ?, ?, ?)",
            (actor, action, target_type, target_id,
             json.dumps(details) if details else None),
        )

    def close(self):
        self.conn.close()


class PostgresDatabase:
    """Real Postgres implementation via psycopg2. Not importable/usable
    without `pip install psycopg2-binary` and a reachable Postgres
    instance — this module only imports psycopg2 lazily, inside
    __init__, so the rest of the codebase (including demo.py, which
    never touches Postgres) still works with zero extra dependencies."""

    def __init__(self, database_url: str):
        try:
            import psycopg2
            import psycopg2.extras
        except ImportError as e:
            raise ImportError(
                "PostgresDatabase requires psycopg2-binary. "
                "Run: pip install -r requirements.txt"
            ) from e
        self._psycopg2 = psycopg2
        self.conn = psycopg2.connect(database_url)
        self.conn.autocommit = False
        self._cursor_factory = psycopg2.extras.RealDictCursor
        self._lock = threading.Lock()
        with open(SCHEMA_POSTGRES_PATH) as f:
            with self.conn.cursor() as cur:
                cur.execute(f.read())
        self.conn.commit()

    @staticmethod
    def _translate(sql: str) -> str:
        # Every query in this codebase is written once, in SQLite dialect.
        # Translate the two SQLite-isms we actually use to their Postgres
        # equivalents here, rather than maintaining two copies of every
        # query in registry.py/banker.py/approval.py/orchestrator.py.
        return sql.replace("?", "%s").replace("datetime('now')", "now()")

    @contextmanager
    def tx(self):
        with self._lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def execute(self, sql, params=()):
        with self._lock:
            with self.conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(self._translate(sql), params)
            self.conn.commit()
            return None

    def query(self, sql, params=()):
        with self._lock:
            with self.conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(self._translate(sql), params)
                return cur.fetchall()

    def query_one(self, sql, params=()):
        with self._lock:
            with self.conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(self._translate(sql), params)
                return cur.fetchone()

    def audit(self, actor: str, action: str, target_type: str = None,
              target_id: str = None, details: dict = None):
        self.execute(
            "INSERT INTO audit_log (actor, action, target_type, target_id, details) "
            "VALUES (?, ?, ?, ?, ?)",
            (actor, action, target_type, target_id,
             json.dumps(details) if details else None),
        )

    def close(self):
        self.conn.close()


def get_database(database_url: str = None):
    """The one factory api.py (or anything new) should call.

    - database_url starting with postgres:// or postgresql:// ->
      PostgresDatabase
    - anything else, or None with DATABASE_URL unset -> local SQLite
      file (holding_os.db in this directory), so you can run the API
      with zero external accounts until you're ready for Postgres.
    """
    url = database_url or os.environ.get("DATABASE_URL")
    if url and (url.startswith("postgres://") or url.startswith("postgresql://")):
        return PostgresDatabase(url)
    sqlite_path = os.path.join(os.path.dirname(__file__), "holding_os.db")
    return Database(sqlite_path)
