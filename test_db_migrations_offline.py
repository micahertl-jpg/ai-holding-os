"""
test_db_migrations_offline.py — proves db.py's column-migration
mechanism actually retrofits a column onto an EXISTING table, the
scenario every other test in this repo misses: every other test
creates its database fresh from the CURRENT schema file, so a missing
`_COLUMN_MIGRATIONS` entry would never fail a single test here while
still leaving a real, already-deployed database permanently broken
after a redeploy -- exactly what happened in production with
paper_portfolios.live_trading_enabled (see db.py's module-level
comment on _COLUMN_MIGRATIONS for the full story). This test exists
specifically to catch that failure mode: it builds a database from an
OLD schema missing a migrated column, then re-opens it with the
CURRENT db.py and asserts the column is really there.
"""

import os
import sqlite3

from db import Database, _COLUMN_MIGRATIONS, _apply_sqlite_column_migrations

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_db_migrations.db")


def test_every_registered_migration_is_actually_missing_from_an_old_table_and_gets_added():
    """The real regression test: build each migrated table WITHOUT its
    migrated column (simulating a database deployed before that column
    existed), confirm it's really missing, then open it through the
    normal Database() constructor and confirm every one is present
    afterward -- proving the fix that made this test necessary in the
    first place actually retrofits an old table, not just a fresh
    one."""
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    # A deliberately bare version of the one migrated table, built
    # WITHOUT its migrated column -- standing in for "a database that
    # was deployed before this column was added to the schema."
    conn = sqlite3.connect(TEST_DB_PATH)
    conn.execute("CREATE TABLE paper_portfolios (id TEXT PRIMARY KEY, business_id TEXT)")
    conn.commit()
    for table, column, _ddl in _COLUMN_MIGRATIONS:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        assert column not in cols, f"test setup bug: {table}.{column} should start missing"
    conn.close()

    db = Database(TEST_DB_PATH)  # this must retrofit every migrated column
    for table, column, _ddl in _COLUMN_MIGRATIONS:
        cols = [r[1] for r in db.conn.execute(f"PRAGMA table_info({table})").fetchall()]
        assert column in cols, f"{table}.{column} was not retrofitted onto the existing table"
    db.conn.close()
    os.remove(TEST_DB_PATH)
    print("PASS: every registered column migration is actually retrofitted onto a table that "
          "existed before that column did -- the exact scenario a fresh-database test misses")


def test_a_fresh_database_already_has_every_migrated_column_via_create_table():
    """Sanity check: a brand-new database (the common case every other
    test in this repo uses) already has every migrated column straight
    from CREATE TABLE, so the migration step is a genuine no-op there,
    never a second, conflicting definition."""
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    db = Database(TEST_DB_PATH)
    for table, column, _ddl in _COLUMN_MIGRATIONS:
        cols = [r[1] for r in db.conn.execute(f"PRAGMA table_info({table})").fetchall()]
        assert column in cols
    db.conn.close()
    os.remove(TEST_DB_PATH)
    print("PASS: a fresh database already has every migrated column via its own CREATE TABLE "
          "-- the migration step is a safe no-op there")


def test_applying_migrations_twice_is_idempotent():
    """Migrations run on EVERY process startup (not just the first) --
    running the same ALTER TABLE check twice must never error just
    because the column is already there the second time."""
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    db = Database(TEST_DB_PATH)
    _apply_sqlite_column_migrations(db.conn)  # a real second application, same as a restart
    for table, column, _ddl in _COLUMN_MIGRATIONS:
        cols = [r[1] for r in db.conn.execute(f"PRAGMA table_info({table})").fetchall()]
        assert column in cols
    db.conn.close()
    os.remove(TEST_DB_PATH)
    print("PASS: applying the same column migrations a second time (every real restart) "
          "never errors, since it's already present")


if __name__ == "__main__":
    test_every_registered_migration_is_actually_missing_from_an_old_table_and_gets_added()
    test_a_fresh_database_already_has_every_migrated_column_via_create_table()
    test_applying_migrations_twice_is_idempotent()
    print("\nAll db.py migration offline tests passed.")
