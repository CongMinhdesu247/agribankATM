"""Regressions for the PostgreSQL DB-API adapter, without a live server."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agribank_recon import database


class PostgresRowFactoryTests(unittest.TestCase):
    def test_cursor_without_result_metadata_can_be_created(self):
        # A newly created psycopg Cursor has no description. This was the
        # startup failure shown by Render, before the first SQL execution.
        maker = database._hybrid_row_factory(SimpleNamespace(description=None))
        self.assertTrue(callable(maker))
        self.assertEqual(maker(()), ())

    def test_result_rows_support_named_numeric_and_dictionary_access(self):
        cursor = SimpleNamespace(description=[SimpleNamespace(name="username"), SimpleNamespace(name="role")])
        maker = database._hybrid_row_factory(cursor)
        row = maker(("operator", "operator"))
        self.assertEqual(row[0], "operator")
        self.assertEqual(row["role"], "operator")
        self.assertEqual(dict(row), {"username": "operator", "role": "operator"})
        self.assertEqual(tuple(row), ("operator", "operator"))

    def test_factory_handles_result_then_non_result_then_new_result(self):
        cursor = SimpleNamespace(description=None)
        database._hybrid_row_factory(cursor)  # construction
        cursor.description = [SimpleNamespace(name="count")]
        first = database._hybrid_row_factory(cursor)
        self.assertEqual(first((3,))["count"], 3)
        cursor.description = None  # CREATE TABLE / INSERT / COMMIT
        self.assertTrue(callable(database._hybrid_row_factory(cursor)))
        cursor.description = [SimpleNamespace(name="status")]
        second = database._hybrid_row_factory(cursor)
        self.assertEqual(second(("ready",))["status"], "ready")
        # Existing row makers keep their own column mapping.
        self.assertEqual(first((4,))["count"], 4)


class PostgresCursorTests(unittest.TestCase):
    def test_csv_export_can_iterate_rows_without_fetching_all(self):
        rows = [database.HybridRow((1, "tx-1"), ("log_id", "trans_id")),
                database.HybridRow((2, "tx-2"), ("log_id", "trans_id"))]
        raw_cursor = Mock()
        raw_cursor.__iter__ = Mock(return_value=iter(rows))
        cursor = database._PostgresCursor(raw_cursor)
        self.assertEqual([row["trans_id"] for row in cursor], ["tx-1", "tx-2"])
        raw_cursor.fetchall.assert_not_called()

    def test_failed_pool_startup_closes_workers_and_allows_retry(self):
        failed_pool = Mock()
        failed_pool.open.side_effect = RuntimeError("connection failed")
        healthy_pool = Mock()
        constructor = Mock(side_effect=[failed_pool, healthy_pool])
        with patch.object(database, "_PG_POOL", None), patch.dict(
            "sys.modules", {"psycopg_pool": SimpleNamespace(ConnectionPool=constructor)}
        ):
            with self.assertRaisesRegex(RuntimeError, "connection failed"):
                database._postgres_connection_pool()
            failed_pool.close.assert_called_once_with()
            self.assertIsNone(database._PG_POOL)
            self.assertIs(database._postgres_connection_pool(), healthy_pool)
            self.assertIs(database._postgres_connection_pool(), healthy_pool)
        self.assertEqual(constructor.call_count, 2)


if __name__ == "__main__":
    unittest.main()
