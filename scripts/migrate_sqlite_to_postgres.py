"""Import a verified SQLite database into a clean PostgreSQL schema."""

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

from agribank_recon import config
from agribank_recon.database import connect_database, get_table_columns
from agribank_recon.reconcile_engine import initialize_database, initialize_run_workflow

TABLES_IN_COPY_ORDER = (
    "reconciliation_runs",
    "reconciliation_results",
    "reconciliation_run_results",
    "audit_logs",
    "reconciliation_baseline",
    "reconciliation_baseline_results",
    "reconciliation_baseline_state",
    "app_users",
    "login_attempts",
)
def _read_source(path: Path):
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    source = {}
    try:
        for table in TABLES_IN_COPY_ORDER:
            if table not in tables:
                source[table] = ([], [])
                continue
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            rows = connection.execute(f"SELECT * FROM {table}").fetchall()
            source[table] = (columns, [tuple(row[column] for column in columns) for row in rows])
    finally:
        connection.close()
    return source


def _target_rows(connection, table: str):
    return connection.execute(f"SELECT * FROM {table}").fetchall()


def _prepare_target(connection, source):
    user_columns, source_users = source["app_users"]
    source_user_by_name = {
        str(dict(zip(user_columns, row))["username"]).casefold(): dict(zip(user_columns, row))
        for row in source_users
    }
    existing_admins = _target_rows(connection, "app_users")
    for row in existing_admins:
        source_user = source_user_by_name.get(str(row["username"]).casefold())
        if source_user is None or any(
            row[column] != source_user[column]
            for column in ("password_hash", "role", "is_active")
        ):
            raise RuntimeError(
                "The PostgreSQL database already has an app_users row that differs from the SQLite source. "
                "Back up the target or align bootstrap admin settings before migrating."
            )
    if existing_admins:
        # Keep source account metadata exactly, including the original creation timestamp.
        connection.execute("DELETE FROM app_users")

    for table in TABLES_IN_COPY_ORDER:
        if table == "app_users":
            continue
        rows = _target_rows(connection, table)
        if table == "reconciliation_baseline_state" and len(rows) == 1:
            # Fresh app startup captures an empty initial baseline. Replace that placeholder
            # with the original baseline from SQLite as part of the migration transaction.
            row = rows[0]
            if row["baseline_id"] == 1 and row["transaction_count"] == 0:
                connection.execute("DELETE FROM reconciliation_baseline_state WHERE baseline_id = 1")
                rows = []
        if rows:
            raise RuntimeError(
                f"PostgreSQL table {table} is not empty. Run this migration only into a new/empty target database."
            )


def migrate(source_path: Path) -> dict[str, int]:
    if config.DATABASE_KIND != "postgresql":
        raise RuntimeError("Set DATABASE_URL to the Railway PostgreSQL connection string before running this migration.")
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite source database not found: {source_path}")

    source = _read_source(source_path)
    initialize_database()
    counts = {}
    with closing(connect_database()) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            _prepare_target(connection, source)

            for table in TABLES_IN_COPY_ORDER:
                columns, rows = source[table]
                if not rows:
                    counts[table] = 0
                    continue
                target_columns = get_table_columns(connection, table)
                unknown_columns = set(columns) - target_columns
                if unknown_columns:
                    raise RuntimeError(
                        f"SQLite table {table} has columns missing from PostgreSQL: {', '.join(sorted(unknown_columns))}"
                    )

                placeholders = ", ".join("?" for _ in columns)
                column_sql = ", ".join(columns)
                connection.executemany(
                    f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})",
                    rows,
                )
                counts[table] = len(rows)

            for table, sequence_column in (("reconciliation_results", "recon_id"), ("audit_logs", "log_id")):
                connection.execute(
                    f"SELECT setval(pg_get_serial_sequence('{table}', '{sequence_column}'), "
                    f"GREATEST(COALESCE((SELECT MAX({sequence_column}) FROM {table}), 1), 1), "
                    f"EXISTS (SELECT 1 FROM {table}))"
                )

            for table in TABLES_IN_COPY_ORDER:
                actual = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                expected = len(source[table][1])
                if actual != expected:
                    raise RuntimeError(f"Row-count verification failed for {table}: expected {expected}, got {actual}")

            if "source_hash" not in source["reconciliation_runs"][0]:
                # The empty target already ran migrations at startup. Upgrade
                # legacy source rows only after verifying the exact copied counts.
                connection.execute("DELETE FROM schema_migrations WHERE version = 2")
                initialize_run_workflow(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Copy application data from SQLite into a clean PostgreSQL database.")
    parser.add_argument("--sqlite-path", required=True, help="Path to a verified SQLite backup")
    args = parser.parse_args()
    counts = migrate(Path(args.sqlite_path))
    print("SQLite to PostgreSQL migration completed and row counts verified.")
    for table, count in counts.items():
        print(f"{table}: {count}")


if __name__ == "__main__":
    main()
