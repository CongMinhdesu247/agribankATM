import sqlite3
import uuid
from contextlib import closing
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import config
from .config import BASE_DIR, DB_PATH
from .database import connect_database, get_table_columns

CORE_LOG_PATH = BASE_DIR / "core_log.csv"
PARTNER_LOG_PATH = BASE_DIR / "partner_log.csv"
MAX_SAFE_VND = 9_007_199_254_740_991


def normalize_vnd_amounts(values: pd.Series, label: str) -> pd.Series:
    amounts: list[int] = []
    for value in values.tolist():
        raw_value = str(value).strip()
        if len(raw_value) > 32:
            raise ValueError(f"{label} amounts must be whole non-negative VND values")
        try:
            amount = Decimal(raw_value)
        except (InvalidOperation, ValueError):
            raise ValueError(f"{label} amounts must be whole non-negative VND values")
        if (not amount.is_finite() or amount < 0 or amount != amount.to_integral_value()
                or amount > MAX_SAFE_VND):
            raise ValueError(f"{label} amounts must be whole non-negative VND values no larger than {MAX_SAFE_VND:,}")
        amounts.append(int(amount))
    return pd.Series(amounts, index=values.index, dtype="int64")


def initialize_database() -> None:
    if config.DATABASE_KIND == "postgresql":
        initialize_postgres_database()
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS reconciliation_results (
                recon_id INTEGER PRIMARY KEY AUTOINCREMENT,
                trans_id TEXT UNIQUE NOT NULL,
                core_amount REAL,
                partner_amount REAL,
                core_status TEXT,
                partner_status TEXT,
                recon_status TEXT NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                run_id TEXT
            );
            CREATE TABLE IF NOT EXISTS reconciliation_runs (
                run_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                performed_by TEXT NOT NULL,
                core_filename TEXT NOT NULL,
                partner_filename TEXT NOT NULL,
                transaction_count INTEGER NOT NULL,
                matched_count INTEGER NOT NULL,
                discrepancy_count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reconciliation_run_results (
                run_id TEXT NOT NULL,
                trans_id TEXT NOT NULL,
                core_amount REAL,
                partner_amount REAL,
                core_status TEXT,
                partner_status TEXT,
                recon_status TEXT NOT NULL,
                PRIMARY KEY (run_id, trans_id),
                FOREIGN KEY (run_id) REFERENCES reconciliation_runs(run_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS audit_logs (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                trans_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                napas_ref_code TEXT NOT NULL,
                reason TEXT NOT NULL,
                performed_by TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                core_amount REAL,
                partner_amount REAL,
                core_status TEXT,
                partner_status TEXT,
                recon_status TEXT,
                run_id TEXT,
                review_status TEXT NOT NULL DEFAULT 'APPROVED',
                approved_by TEXT,
                approved_at TEXT,
                approval_comment TEXT
            );
            CREATE TABLE IF NOT EXISTS reconciliation_baseline (
                trans_id TEXT PRIMARY KEY,
                core_amount REAL,
                partner_amount REAL,
                core_status TEXT,
                partner_status TEXT,
                recon_status TEXT NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS reconciliation_baseline_state (
                baseline_id INTEGER PRIMARY KEY CHECK (baseline_id = 1),
                captured_at TEXT NOT NULL,
                transaction_count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reconciliation_baseline_results (
                trans_id TEXT PRIMARY KEY,
                core_amount REAL,
                partner_amount REAL,
                core_status TEXT,
                partner_status TEXT,
                recon_status TEXT NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                run_id TEXT
            );
            CREATE TABLE IF NOT EXISTS app_users (
                username TEXT PRIMARY KEY COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin', 'operator', 'approver')),
                is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS login_attempts (
                ip_hash TEXT NOT NULL,
                username_hash TEXT,
                attempted_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS app_sessions (
                token_hash TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_app_sessions_username ON app_sessions (username);
            CREATE INDEX IF NOT EXISTS idx_app_sessions_expiry ON app_sessions (expires_at);
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_reconciliation_status_resolved
                ON reconciliation_results (recon_status, resolved);
            CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at
                ON audit_logs (created_at, log_id);
        """)
        _ensure_column(conn, "reconciliation_results", "run_id", "TEXT")
        _ensure_column(conn, "audit_logs", "run_id", "TEXT")
        _ensure_column(conn, "audit_logs", "review_status", "TEXT NOT NULL DEFAULT 'APPROVED'")
        _ensure_column(conn, "audit_logs", "approved_by", "TEXT")
        _ensure_column(conn, "audit_logs", "approved_at", "TEXT")
        _ensure_column(conn, "audit_logs", "approval_comment", "TEXT")
        _ensure_column(conn, "login_attempts", "username_hash", "TEXT")
        _ensure_audit_snapshot_columns(conn)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_audit_logs_review_lookup
            ON audit_logs (trans_id, run_id, review_status)
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_time ON login_attempts (ip_hash, attempted_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_username_time ON login_attempts (username_hash, attempted_at)")
        baseline_exists = conn.execute(
            "SELECT 1 FROM reconciliation_baseline_state WHERE baseline_id = 1"
        ).fetchone()
        if baseline_exists is None:
            legacy_rows = conn.execute("""
                SELECT trans_id, core_amount, partner_amount, core_status, partner_status,
                       recon_status, resolved, created_at
                FROM reconciliation_baseline
            """).fetchall()
            snapshot_rows = legacy_rows
            include_run_id = False
            if not snapshot_rows:
                # Older app versions populated an empty baseline from the active
                # dataset. Preserve that behavior when migrating a populated DB.
                snapshot_rows = conn.execute("""
                    SELECT trans_id, core_amount, partner_amount, core_status, partner_status,
                           recon_status, resolved, created_at, run_id
                    FROM reconciliation_results
                """).fetchall()
                include_run_id = True
            conn.execute("""
                INSERT INTO reconciliation_baseline_state (baseline_id, captured_at, transaction_count)
                VALUES (1, CURRENT_TIMESTAMP, ?)
            """, (len(snapshot_rows),))
            if snapshot_rows and include_run_id:
                conn.executemany("""
                    INSERT INTO reconciliation_baseline_results
                        (trans_id, core_amount, partner_amount, core_status, partner_status,
                         recon_status, resolved, created_at, run_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, snapshot_rows)
            elif snapshot_rows:
                conn.executemany("""
                    INSERT INTO reconciliation_baseline_results
                        (trans_id, core_amount, partner_amount, core_status, partner_status,
                         recon_status, resolved, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, snapshot_rows)
        conn.execute("INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (1, CURRENT_TIMESTAMP)")
        conn.commit()


def initialize_postgres_database() -> None:
    """Create or upgrade the PostgreSQL schema using portable text timestamps."""
    schema = (
        """CREATE TABLE IF NOT EXISTS reconciliation_results (
            recon_id BIGSERIAL PRIMARY KEY,
            trans_id TEXT UNIQUE NOT NULL,
            core_amount DOUBLE PRECISION,
            partner_amount DOUBLE PRECISION,
            core_status TEXT,
            partner_status TEXT,
            recon_status TEXT NOT NULL,
            resolved INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
            run_id TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS reconciliation_runs (
            run_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            performed_by TEXT NOT NULL,
            core_filename TEXT NOT NULL,
            partner_filename TEXT NOT NULL,
            transaction_count INTEGER NOT NULL,
            matched_count INTEGER NOT NULL,
            discrepancy_count INTEGER NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS reconciliation_run_results (
            run_id TEXT NOT NULL REFERENCES reconciliation_runs(run_id) ON DELETE CASCADE,
            trans_id TEXT NOT NULL,
            core_amount DOUBLE PRECISION,
            partner_amount DOUBLE PRECISION,
            core_status TEXT,
            partner_status TEXT,
            recon_status TEXT NOT NULL,
            PRIMARY KEY (run_id, trans_id)
        )""",
        """CREATE TABLE IF NOT EXISTS audit_logs (
            log_id BIGSERIAL PRIMARY KEY,
            trans_id TEXT NOT NULL,
            action_type TEXT NOT NULL,
            napas_ref_code TEXT NOT NULL,
            reason TEXT NOT NULL,
            performed_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
            core_amount DOUBLE PRECISION,
            partner_amount DOUBLE PRECISION,
            core_status TEXT,
            partner_status TEXT,
            recon_status TEXT,
            run_id TEXT,
            review_status TEXT NOT NULL DEFAULT 'APPROVED',
            approved_by TEXT,
            approved_at TEXT,
            approval_comment TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS reconciliation_baseline (
            trans_id TEXT PRIMARY KEY,
            core_amount DOUBLE PRECISION,
            partner_amount DOUBLE PRECISION,
            core_status TEXT,
            partner_status TEXT,
            recon_status TEXT NOT NULL,
            resolved INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
        )""",
        """CREATE TABLE IF NOT EXISTS reconciliation_baseline_state (
            baseline_id INTEGER PRIMARY KEY CHECK (baseline_id = 1),
            captured_at TEXT NOT NULL,
            transaction_count INTEGER NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS reconciliation_baseline_results (
            trans_id TEXT PRIMARY KEY,
            core_amount DOUBLE PRECISION,
            partner_amount DOUBLE PRECISION,
            core_status TEXT,
            partner_status TEXT,
            recon_status TEXT NOT NULL,
            resolved INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (CURRENT_TIMESTAMP::text),
            run_id TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS app_users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin', 'operator', 'approver')),
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS login_attempts (
            ip_hash TEXT NOT NULL,
            username_hash TEXT,
            attempted_at DOUBLE PRECISION NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS app_sessions (
            token_hash TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            expires_at DOUBLE PRECISION NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_app_sessions_username ON app_sessions (username)",
        "CREATE INDEX IF NOT EXISTS idx_app_sessions_expiry ON app_sessions (expires_at)",
        """CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )""",
    )
    with closing(connect_database()) as conn:
        try:
            for statement in schema:
                conn.execute(statement)
            _ensure_column(conn, "reconciliation_results", "run_id", "TEXT")
            _ensure_column(conn, "audit_logs", "run_id", "TEXT")
            _ensure_column(conn, "audit_logs", "review_status", "TEXT NOT NULL DEFAULT 'APPROVED'")
            _ensure_column(conn, "audit_logs", "approved_by", "TEXT")
            _ensure_column(conn, "audit_logs", "approved_at", "TEXT")
            _ensure_column(conn, "audit_logs", "approval_comment", "TEXT")
            _ensure_column(conn, "login_attempts", "username_hash", "TEXT")
            _ensure_audit_snapshot_columns(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_reconciliation_status_resolved ON reconciliation_results (recon_status, resolved)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs (created_at, log_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_review_lookup ON audit_logs (trans_id, run_id, review_status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_time ON login_attempts (ip_hash, attempted_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_username_time ON login_attempts (username_hash, attempted_at)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_app_users_username_lower ON app_users (LOWER(username))")
            baseline_exists = conn.execute(
                "SELECT 1 FROM reconciliation_baseline_state WHERE baseline_id = 1"
            ).fetchone()
            if baseline_exists is None:
                legacy_rows = conn.execute("""
                    SELECT trans_id, core_amount, partner_amount, core_status, partner_status,
                           recon_status, resolved, created_at
                    FROM reconciliation_baseline
                """).fetchall()
                snapshot_rows = legacy_rows
                include_run_id = False
                if not snapshot_rows:
                    snapshot_rows = conn.execute("""
                        SELECT trans_id, core_amount, partner_amount, core_status, partner_status,
                               recon_status, resolved, created_at, run_id
                        FROM reconciliation_results
                    """).fetchall()
                    include_run_id = True
                conn.execute("""
                    INSERT INTO reconciliation_baseline_state (baseline_id, captured_at, transaction_count)
                    VALUES (1, CURRENT_TIMESTAMP::text, ?)
                """, (len(snapshot_rows),))
                if snapshot_rows and include_run_id:
                    conn.executemany("""
                        INSERT INTO reconciliation_baseline_results
                            (trans_id, core_amount, partner_amount, core_status, partner_status,
                             recon_status, resolved, created_at, run_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, [tuple(row) for row in snapshot_rows])
                elif snapshot_rows:
                    conn.executemany("""
                        INSERT INTO reconciliation_baseline_results
                            (trans_id, core_amount, partner_amount, core_status, partner_status,
                             recon_status, resolved, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, [tuple(row) for row in snapshot_rows])
            conn.execute("INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (1, CURRENT_TIMESTAMP::text)")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
def _ensure_column(conn: sqlite3.Connection, table: str, name: str, column_type: str) -> None:
    existing = get_table_columns(conn, table)
    if name not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}")


def _ensure_audit_snapshot_columns(conn: sqlite3.Connection) -> None:
    existing = get_table_columns(conn, "audit_logs")
    columns = {
        "core_amount": "REAL",
        "partner_amount": "REAL",
        "core_status": "TEXT",
        "partner_status": "TEXT",
        "recon_status": "TEXT",
    }
    for name, column_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE audit_logs ADD COLUMN {name} {column_type}")


def build_reconciliation_rows(core: pd.DataFrame, partner: pd.DataFrame) -> list[tuple]:
    core = core.copy()
    partner = partner.copy()
    core["amount"] = normalize_vnd_amounts(core["amount"], "Core")
    partner["amount"] = normalize_vnd_amounts(partner["amount"], "Partner")
    merged = pd.merge(core, partner, on="trans_id", how="outer", suffixes=("_core", "_partner"))
    if merged.empty:
        raise ValueError("The uploaded CSV files do not contain any transactions.")

    core_amount = merged["amount_core"]
    partner_amount = merged["amount_partner"]
    core_status = merged["status_core"]
    partner_status = merged["status_partner"]
    missing_core = core_amount.isna()
    missing_partner = partner_amount.isna()
    same_amount = core_amount.eq(partner_amount).fillna(False)

    reconciliation_status = pd.Series("MATCHED", index=merged.index, dtype="string")
    reconciliation_status.loc[missing_core] = "MISSING_IN_CORE"
    reconciliation_status.loc[~missing_core & missing_partner] = "MISSING_IN_PARTNER"
    reconciliation_status.loc[~missing_core & ~missing_partner & ~same_amount] = "AMOUNT_MISMATCH"
    reconciliation_status.loc[
        ~missing_core & ~missing_partner & same_amount & core_status.ne(partner_status).fillna(False)
    ] = "STATUS_MISMATCH"

    def nullable(value):
        return None if pd.isna(value) else value

    return [
        (
            str(trans_id),
            None if pd.isna(c_amount) else int(c_amount),
            None if pd.isna(p_amount) else int(p_amount),
            nullable(c_status), nullable(p_status), str(status),
        )
        for trans_id, c_amount, p_amount, c_status, p_status, status in zip(
            merged["trans_id"], core_amount, partner_amount, core_status, partner_status, reconciliation_status
        )
    ]


def replace_reconciliation_results(
    results: list[tuple], *, run_id: str | None = None, performed_by: str = "system",
    core_filename: str = "core_log.csv", partner_filename: str = "partner_log.csv",
    expected_current_run_id: str | None = None, enforce_expected: bool = False,
) -> str:
    if not results:
        raise ValueError("No reconciliation results were produced.")
    initialize_database()
    run_id = run_id or uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    matched_count = sum(1 for row in results if row[5] == "MATCHED")
    with closing(connect_database(DB_PATH)) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT run_id FROM reconciliation_results LIMIT 1").fetchone()
            active_run_id = current[0] if current else None
            if enforce_expected and active_run_id != expected_current_run_id:
                raise ValueError("The active reconciliation changed after preview; preview the files again")
            baseline = conn.execute("SELECT 1 FROM reconciliation_baseline_state WHERE baseline_id = 1").fetchone()
            if baseline is None:
                current = conn.execute("""
                    SELECT trans_id, core_amount, partner_amount, core_status, partner_status,
                           recon_status, resolved, created_at, run_id
                    FROM reconciliation_results
                """).fetchall()
                conn.execute("""
                    INSERT INTO reconciliation_baseline_state (baseline_id, captured_at, transaction_count)
                    VALUES (1, ?, ?)
                """, (created_at, len(current)))
                conn.executemany("""
                    INSERT INTO reconciliation_baseline_results
                        (trans_id, core_amount, partner_amount, core_status, partner_status,
                         recon_status, resolved, created_at, run_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, current)
            conn.execute("""
                INSERT INTO reconciliation_runs
                    (run_id, created_at, performed_by, core_filename, partner_filename,
                     transaction_count, matched_count, discrepancy_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (run_id, created_at, performed_by, core_filename, partner_filename,
                  len(results), matched_count, len(results) - matched_count))
            conn.execute("""
                UPDATE audit_logs SET review_status = 'SUPERSEDED',
                    approval_comment = 'Superseded by a newer reconciliation run'
                WHERE review_status = 'PENDING'
            """)
            conn.executemany("""
                INSERT INTO reconciliation_run_results
                    (run_id, trans_id, core_amount, partner_amount, core_status, partner_status, recon_status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, [(run_id, *row) for row in results])
            conn.execute("DELETE FROM reconciliation_results")
            conn.executemany("""
                INSERT INTO reconciliation_results
                    (trans_id, core_amount, partner_amount, core_status, partner_status, recon_status, run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, [(*row, run_id) for row in results])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return run_id


def run_reconciliation(core_path: Path = CORE_LOG_PATH, partner_path: Path = PARTNER_LOG_PATH) -> None:
    core = pd.read_csv(core_path, dtype={"trans_id": "string", "amount": "string"})
    partner = pd.read_csv(partner_path, dtype={"trans_id": "string", "amount": "string"})
    rows = build_reconciliation_rows(core, partner)
    replace_reconciliation_results(rows, performed_by="system", core_filename=core_path.name,
                                   partner_filename=partner_path.name)
    print(f"Reconciled {len(rows)} distinct transactions.")


if __name__ == "__main__":
    run_reconciliation()

