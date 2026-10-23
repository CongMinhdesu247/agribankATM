"""Small DB-API compatibility layer for SQLite and PostgreSQL."""

from __future__ import annotations

import re
import sqlite3
from threading import Lock
from typing import Any

import config

_PG_LOCK_KEY = 731_882_916_204
_PG_POOL = None
_PG_POOL_LOCK = Lock()


class HybridRow:
    """A row that supports SQLite-style numeric and named access."""

    __slots__ = ("_values", "_names", "_indexes")

    def __init__(self, values, names):
        self._values = tuple(values)
        self._names = tuple(names)
        self._indexes = {name: index for index, name in enumerate(self._names)}

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._values[self._indexes[key]]
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def keys(self):
        return self._names


def _hybrid_row_factory(cursor):
    names = tuple(column.name for column in cursor.description)
    return lambda values: HybridRow(values, names)


def _postgres_connection_pool():
    global _PG_POOL
    if _PG_POOL is None:
        with _PG_POOL_LOCK:
            if _PG_POOL is None:
                try:
                    from psycopg_pool import ConnectionPool
                except ImportError as error:
                    raise RuntimeError(
                        "PostgreSQL support requires psycopg[binary,pool]; install project requirements."
                    ) from error
                pool = ConnectionPool(
                    conninfo=config.DATABASE_URL,
                    min_size=1,
                    max_size=10,
                    timeout=10,
                    kwargs={"row_factory": _hybrid_row_factory},
                    open=False,
                )
                pool.open(wait=True, timeout=10)
                _PG_POOL = pool
    return _PG_POOL


def translate_postgres_sql(sql: str) -> tuple[str, tuple[Any, ...] | None]:
    """Translate the small set of SQLite syntax used by this application."""
    statement = sql.strip().rstrip(";")
    if statement.upper() == "BEGIN IMMEDIATE":
        # Keep SQLite's serialized-writer behavior for critical write flows.
        return "SELECT pg_advisory_xact_lock(%s)", (_PG_LOCK_KEY,)
    if statement.upper().startswith("PRAGMA "):
        return "SELECT 1", ()

    ignore_conflict = bool(re.match(r"(?is)^INSERT\s+OR\s+IGNORE\s+INTO\s+", statement))
    if ignore_conflict:
        statement = re.sub(r"(?is)^INSERT\s+OR\s+IGNORE\s+INTO\s+", "INSERT INTO ", statement, count=1)

    # SQLite's `x IS ?` is null-safe equality. PostgreSQL spells it out.
    statement = re.sub(
        r"(?i)(\b(?:[A-Za-z_][A-Za-z0-9_]*\.)?[A-Za-z_][A-Za-z0-9_]*)\s+IS\s+\?",
        r"\1 IS NOT DISTINCT FROM ?",
        statement,
    )
    statement = re.sub(r"(?i)\bCURRENT_TIMESTAMP\b(?!\s*::text)", "CURRENT_TIMESTAMP::text", statement)
    statement = statement.replace("?", "%s")
    if ignore_conflict:
        statement += " ON CONFLICT DO NOTHING"
    return statement, None


class _PostgresCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def execute(self, sql: str, parameters=()):
        statement, translated_parameters = translate_postgres_sql(sql)
        self._cursor.execute(statement, translated_parameters if translated_parameters is not None else tuple(parameters or ()))
        return self

    def executemany(self, sql: str, parameters):
        statement, translated_parameters = translate_postgres_sql(sql)
        if translated_parameters is not None:
            raise ValueError("This statement cannot be used with executemany")
        self._cursor.executemany(statement, parameters)
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def close(self):
        self._cursor.close()


class _PostgresConnection:
    def __init__(self, pool, connection):
        self._pool = pool
        self._connection = connection
        self._closed = False

    def execute(self, sql: str, parameters=()):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql: str, parameters):
        return self.cursor().executemany(sql, parameters)

    def cursor(self):
        return _PostgresCursor(self._connection.cursor())

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        if not self._closed:
            self._closed = True
            self._pool.putconn(self._connection)


def connect_database(sqlite_path=None):
    if config.DATABASE_KIND == "sqlite":
        path = sqlite_path if sqlite_path is not None else config.DB_PATH
        connection = sqlite3.connect(path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    pool = _postgres_connection_pool()
    return _PostgresConnection(pool, pool.getconn(timeout=10))


def get_table_columns(connection, table_name: str) -> set[str]:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name):
        raise ValueError("Invalid table name")
    if config.DATABASE_KIND == "sqlite":
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})")}
    rows = connection.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ?",
        (table_name,),
    ).fetchall()
    return {row[0] for row in rows}


def is_integrity_error(error: Exception) -> bool:
    if isinstance(error, sqlite3.IntegrityError):
        return True
    try:
        import psycopg
    except ImportError:
        return False
    return isinstance(error, psycopg.IntegrityError)


def close_database_pool() -> None:
    global _PG_POOL
    with _PG_POOL_LOCK:
        if _PG_POOL is not None:
            _PG_POOL.close()
            _PG_POOL = None
