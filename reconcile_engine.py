import sqlite3
from pathlib import Path

import pandas as pd

from config import BASE_DIR, DB_PATH

CORE_LOG_PATH = BASE_DIR / "core_log.csv"
PARTNER_LOG_PATH = BASE_DIR / "partner_log.csv"


def initialize_database() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
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
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
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
                recon_status TEXT
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
            CREATE INDEX IF NOT EXISTS idx_reconciliation_status_resolved
                ON reconciliation_results (recon_status, resolved);
            CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at
                ON audit_logs (created_at, log_id);
        """)
        _ensure_audit_snapshot_columns(conn)


def _ensure_audit_snapshot_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(audit_logs)")}
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
            None if pd.isna(c_amount) else float(c_amount),
            None if pd.isna(p_amount) else float(p_amount),
            nullable(c_status), nullable(p_status), str(status),
        )
        for trans_id, c_amount, p_amount, c_status, p_status, status in zip(
            merged["trans_id"], core_amount, partner_amount, core_status, partner_status, reconciliation_status
        )
    ]


def replace_reconciliation_results(results: list[tuple]) -> None:
    if not results:
        raise ValueError("No reconciliation results were produced.")
    initialize_database()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM reconciliation_results")
            conn.executemany("""
                INSERT INTO reconciliation_results
                    (trans_id, core_amount, partner_amount, core_status, partner_status, recon_status)
                VALUES (?, ?, ?, ?, ?, ?)
            """, results)
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def run_reconciliation(core_path: Path = CORE_LOG_PATH, partner_path: Path = PARTNER_LOG_PATH) -> None:
    core = pd.read_csv(core_path, dtype={"trans_id": "string"})
    partner = pd.read_csv(partner_path, dtype={"trans_id": "string"})
    rows = build_reconciliation_rows(core, partner)
    replace_reconciliation_results(rows)
    print(f"Reconciled {len(rows)} distinct transactions.")


if __name__ == "__main__":
    run_reconciliation()

