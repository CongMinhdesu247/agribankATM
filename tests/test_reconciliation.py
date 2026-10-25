import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from agribank_recon import reconcile_engine
from agribank_recon.security import hash_password, verify_password


class AmountValidationTests(unittest.TestCase):
    def test_whole_vnd_values_remain_exact(self):
        values = pd.Series(["9007199254740991", "9007199254740990", "12.0"])
        self.assertEqual(
            reconcile_engine.normalize_vnd_amounts(values, "Core").tolist(),
            [9007199254740991, 9007199254740990, 12],
        )

    def test_fractional_negative_non_finite_and_unsafe_values_are_rejected(self):
        for value in ("1.25", "-1", "NaN", "Infinity", "9007199254740992", "bad"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                reconcile_engine.normalize_vnd_amounts(pd.Series([value]), "Core")

    def test_extremely_long_amount_is_rejected_before_decimal_parsing(self):
        with self.assertRaises(ValueError):
            reconcile_engine.normalize_vnd_amounts(pd.Series(["9" * 100_000]), "Core")


class PasswordHashTests(unittest.TestCase):
    def test_password_hash_verifies_and_rejects_untrusted_work_factors(self):
        encoded = hash_password("a sufficiently long password")
        self.assertTrue(verify_password("a sufficiently long password", encoded))
        self.assertFalse(verify_password("wrong password", encoded))
        self.assertFalse(verify_password("anything", "scrypt$999999999$8$1$YWJj$YWJj"))


class ReconciliationRulesTests(unittest.TestCase):
    def test_match_mismatch_and_missing_categories(self):
        core = pd.DataFrame([
            ("same", "100", "SUCCESS"),
            ("amount", "200", "SUCCESS"),
            ("status", "300", "SUCCESS"),
            ("partner_missing", "400", "SUCCESS"),
        ], columns=["trans_id", "amount", "status"])
        partner = pd.DataFrame([
            ("same", "100", "SUCCESS"),
            ("amount", "201", "SUCCESS"),
            ("status", "300", "PENDING"),
            ("core_missing", "500", "SUCCESS"),
        ], columns=["trans_id", "amount", "status"])
        rows = reconcile_engine.build_reconciliation_rows(core, partner)
        statuses = {row[0]: row[5] for row in rows}
        self.assertEqual(statuses, {
            "same": "MATCHED", "amount": "AMOUNT_MISMATCH", "status": "STATUS_MISMATCH",
            "partner_missing": "MISSING_IN_PARTNER", "core_missing": "MISSING_IN_CORE",
        })


class ReconciliationStorageTests(unittest.TestCase):
    def test_legacy_workflow_upgrade_preserves_resolutions_and_pending_reviews(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy-workflow.db"
            with patch.object(reconcile_engine, "DB_PATH", path):
                reconcile_engine.initialize_database()
                connection = sqlite3.connect(path)
                try:
                    connection.execute("DELETE FROM schema_migrations WHERE version = 2")
                    connection.executemany("""INSERT INTO reconciliation_results
                        (trans_id, recon_status, resolved) VALUES (?, 'AMOUNT_MISMATCH', ?)""",
                        [("legacy-pending", 0), ("legacy-approved", 1)])
                    connection.execute("""INSERT INTO audit_logs
                        (trans_id, action_type, napas_ref_code, reason, performed_by, review_status)
                        VALUES ('legacy-pending', 'REFUND', 'ref', 'reason', 'operator', 'PENDING')""")
                    connection.commit()
                finally:
                    connection.close()
                reconcile_engine.initialize_database()
                reconcile_engine.initialize_database()
                connection = sqlite3.connect(path)
                try:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM reconciliation_runs").fetchone()[0], 1)
                    run_id = connection.execute("SELECT run_id FROM reconciliation_runs").fetchone()[0]
                    self.assertEqual(connection.execute("SELECT run_id, review_status FROM audit_logs").fetchone(),
                                     (run_id, "PENDING"))
                    self.assertEqual(connection.execute("SELECT resolved FROM reconciliation_results WHERE trans_id = 'legacy-approved'").fetchone()[0], 1)
                    self.assertEqual(connection.execute("SELECT resolved FROM reconciliation_run_results WHERE trans_id = 'legacy-approved'").fetchone()[0], 1)
                    self.assertIsNone(connection.execute("SELECT closed_at FROM reconciliation_runs").fetchone()[0])
                    self.assertIsNotNone(connection.execute("SELECT source_hash FROM reconciliation_runs").fetchone()[0])
                finally:
                    connection.close()

    def test_existing_database_schema_migrates_idempotently(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "legacy.db"
            connection = sqlite3.connect(db_path)
            try:
                connection.executescript("""
                    CREATE TABLE reconciliation_results (
                        recon_id INTEGER PRIMARY KEY, trans_id TEXT UNIQUE NOT NULL,
                        core_amount REAL, partner_amount REAL, core_status TEXT, partner_status TEXT,
                        recon_status TEXT NOT NULL, resolved INTEGER NOT NULL DEFAULT 0,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE audit_logs (
                        log_id INTEGER PRIMARY KEY, trans_id TEXT NOT NULL, action_type TEXT NOT NULL,
                        napas_ref_code TEXT NOT NULL, reason TEXT NOT NULL, performed_by TEXT NOT NULL,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE reconciliation_baseline (
                        trans_id TEXT PRIMARY KEY, core_amount REAL, partner_amount REAL,
                        core_status TEXT, partner_status TEXT, recon_status TEXT NOT NULL,
                        resolved INTEGER NOT NULL DEFAULT 0, created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    INSERT INTO reconciliation_baseline
                        (trans_id, core_amount, partner_amount, core_status, partner_status, recon_status)
                    VALUES ('BASE-1', 10, 10, 'SUCCESS', 'SUCCESS', 'MATCHED');
                """)
            finally:
                connection.close()
            with patch.object(reconcile_engine, "DB_PATH", db_path):
                reconcile_engine.initialize_database()
                reconcile_engine.initialize_database()
            connection = sqlite3.connect(db_path)
            try:
                result_columns = {row[1] for row in connection.execute("PRAGMA table_info(reconciliation_results)")}
                audit_columns = {row[1] for row in connection.execute("PRAGMA table_info(audit_logs)")}
                self.assertIn("run_id", result_columns)
                self.assertIn("review_status", audit_columns)
                self.assertIn("approval_comment", audit_columns)
                self.assertEqual(connection.execute("SELECT version FROM schema_migrations").fetchone(), (1,))
                self.assertEqual(connection.execute("SELECT trans_id FROM reconciliation_baseline_results").fetchone(), ("BASE-1",))
                self.assertEqual(connection.execute(
                    "SELECT transaction_count FROM reconciliation_baseline_state WHERE baseline_id = 1"
                ).fetchone(), (1,))
            finally:
                connection.close()

    def test_empty_legacy_baseline_is_migrated_as_an_empty_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "empty-legacy.db"
            connection = sqlite3.connect(db_path)
            try:
                connection.execute("""
                    CREATE TABLE reconciliation_baseline (
                        trans_id TEXT PRIMARY KEY, core_amount REAL, partner_amount REAL,
                        core_status TEXT, partner_status TEXT, recon_status TEXT NOT NULL,
                        resolved INTEGER NOT NULL DEFAULT 0,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                connection.commit()
            finally:
                connection.close()
            with patch.object(reconcile_engine, "DB_PATH", db_path):
                reconcile_engine.initialize_database()
                connection = sqlite3.connect(db_path)
                try:
                    self.assertEqual(connection.execute(
                        "SELECT transaction_count FROM reconciliation_baseline_state WHERE baseline_id = 1"
                    ).fetchone(), (0,))
                finally:
                    connection.close()

    def test_populated_database_with_empty_legacy_baseline_uses_active_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "populated-legacy.db"
            connection = sqlite3.connect(db_path)
            try:
                connection.executescript("""
                    CREATE TABLE reconciliation_results (
                        recon_id INTEGER PRIMARY KEY, trans_id TEXT UNIQUE NOT NULL,
                        core_amount REAL, partner_amount REAL, core_status TEXT, partner_status TEXT,
                        recon_status TEXT NOT NULL, resolved INTEGER NOT NULL DEFAULT 0,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE reconciliation_baseline (
                        trans_id TEXT PRIMARY KEY, core_amount REAL, partner_amount REAL,
                        core_status TEXT, partner_status TEXT, recon_status TEXT NOT NULL,
                        resolved INTEGER NOT NULL DEFAULT 0,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    INSERT INTO reconciliation_results
                        (trans_id, core_amount, partner_amount, core_status, partner_status, recon_status)
                    VALUES ('ACTIVE-1', 25, 25, 'SUCCESS', 'SUCCESS', 'MATCHED');
                """)
                connection.commit()
            finally:
                connection.close()
            with patch.object(reconcile_engine, "DB_PATH", db_path):
                reconcile_engine.initialize_database()
            connection = sqlite3.connect(db_path)
            try:
                self.assertEqual(connection.execute(
                    "SELECT trans_id FROM reconciliation_baseline_results"
                ).fetchone(), ("ACTIVE-1",))
            finally:
                connection.close()

    def test_run_history_and_empty_initial_baseline_are_saved_atomically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "test.db"
            with patch.object(reconcile_engine, "DB_PATH", db_path):
                reconcile_engine.initialize_database()
                first_id = reconcile_engine.replace_reconciliation_results(
                    [("TX-1", 100, 100, "SUCCESS", "SUCCESS", "MATCHED")],
                    run_id="run-1", performed_by="operator", core_filename="core.csv",
                    partner_filename="partner.csv",
                )
                conn = sqlite3.connect(db_path)
                try:
                    baseline = conn.execute(
                        "SELECT transaction_count FROM reconciliation_baseline_state WHERE baseline_id = 1"
                    ).fetchone()
                    self.assertEqual(baseline, (0,))
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM reconciliation_baseline_results").fetchone(), (0,))
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM reconciliation_run_results WHERE run_id = ?", (first_id,)).fetchone(), (1,))
                finally:
                    conn.close()
                reconcile_engine.close_reconciliation_run(first_id, "admin")
                reconcile_engine.replace_reconciliation_results(
                    [("TX-2", 50, 60, "SUCCESS", "SUCCESS", "AMOUNT_MISMATCH")],
                    run_id="run-2", performed_by="operator",
                )
                conn = sqlite3.connect(db_path)
                try:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM reconciliation_runs").fetchone(), (2,))
                    self.assertEqual(conn.execute("SELECT trans_id FROM reconciliation_results").fetchone(), ("TX-2",))
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM reconciliation_baseline_state").fetchone(), (1,))
                finally:
                    conn.close()
                with self.assertRaisesRegex(ValueError, "changed after preview"):
                    reconcile_engine.replace_reconciliation_results(
                        [("TX-3", 100, 100, "SUCCESS", "SUCCESS", "MATCHED")],
                        run_id="stale-run", expected_current_run_id="run-1", enforce_expected=True,
                    )
                conn = sqlite3.connect(db_path)
                try:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM reconciliation_runs").fetchone(), (2,))
                    self.assertEqual(conn.execute("SELECT trans_id FROM reconciliation_results").fetchone(), ("TX-2",))
                finally:
                    conn.close()


if __name__ == "__main__":
    unittest.main()
