"""Security regressions against the real ASGI application and isolated databases."""

import asyncio
import importlib
import json
import csv
import io
import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi import HTTPException

from agribank_recon import config, reconcile_engine, suspensions
from agribank_recon.security import create_access_token, hash_password, verify_access_token


class SecurityTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "security-test-password-only"
        cls.password_hash = hash_password(cls.password)
        # Importing app initializes its database. Never point it at the user's DB.
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            db_path = Path(directory) / "import.db"
            stack.enter_context(patch.multiple(
                config, DATABASE_KIND="sqlite", DB_PATH=db_path, JWT_SECRET="test-only-" * 8,
                APP_USERNAME="admin", APP_PASSWORD_HASH=cls.password_hash,
            ))
            stack.enter_context(patch.object(reconcile_engine, "DB_PATH", db_path))
            cls.web = importlib.import_module("agribank_recon.app")

    def setUp(self):
        stack = self.enterContext(ExitStack())
        directory = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.db_path = directory / "security.db"
        stack.enter_context(patch.multiple(
            config, DATABASE_KIND="sqlite", DB_PATH=self.db_path, JWT_SECRET="test-only-" * 8,
            APP_USERNAME="admin", APP_PASSWORD_HASH=self.password_hash,
            UPLOAD_DIR=directory / "uploads", COOKIE_SECURE=False,
        ))
        stack.enter_context(patch.multiple(self.web, DB_PATH=self.db_path, BASE_DIR=directory))
        stack.enter_context(patch.object(reconcile_engine, "DB_PATH", self.db_path))
        reconcile_engine.initialize_database()
        self.web.seed_admin_user()

    async def request(self, path, *, method="GET", data=None, body=None, headers=None, ip="127.0.0.1"):
        parsed = urlsplit(path)
        supplied_headers = {"host": "localhost", **(headers or {})}
        if data is not None:
            body = json.dumps(data).encode()
            supplied_headers.setdefault("content-type", "application/json")
        chunks = [body or b""]
        sent = []
        complete = asyncio.Event()

        async def receive():
            if chunks:
                return {"type": "http.request", "body": chunks.pop(0), "more_body": False}
            await complete.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                complete.set()

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "scheme": "http", "path": parsed.path,
            "raw_path": parsed.path.encode(), "root_path": "", "query_string": parsed.query.encode(),
            "headers": [(key.encode(), value.encode()) for key, value in supplied_headers.items()],
            "client": (ip, 1234), "server": ("localhost", 80),
        }
        await asyncio.wait_for(self.web.app(scope, receive, send), timeout=10)
        start = next(message for message in sent if message["type"] == "http.response.start")
        response_body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
        response_headers = {key.decode(): value.decode() for key, value in start["headers"]}
        return start["status"], response_headers, response_body

    async def login(self, username="admin", password=None, **kwargs):
        return await self.request("/api/login", method="POST", data={
            "username": username, "password": self.password if password is None else password,
        }, **kwargs)

    async def token(self, username="admin"):
        status, headers, _ = await self.login(username)
        self.assertEqual(status, 200)
        cookie = SimpleCookie(headers["set-cookie"])
        return cookie["agribank_access"].value

    def add_user(self, username, role):
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "INSERT INTO app_users VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP, 'test')",
                (username, self.password_hash, role),
            )

    async def test_login_and_logout_invalidate_only_the_current_session(self):
        first, second = await self.token(), await self.token()
        self.assertNotEqual(first, second)
        self.assertEqual((await self.request("/api/me", headers={"authorization": f"Bearer {first}"}))[0], 200)
        self.assertEqual((await self.request("/api/logout", method="POST", headers={
            "cookie": f"agribank_access={first}", "origin": "http://localhost",
        }))[0], 200)
        for header in ({"authorization": f"Bearer {first}"}, {"cookie": f"agribank_access={first}"}):
            self.assertEqual((await self.request("/api/me", headers=header))[0], 401)
        self.assertEqual((await self.request("/api/me", headers={"authorization": f"Bearer {second}"}))[0], 200)

    async def test_unsigned_tampered_and_unregistered_tokens_are_rejected(self):
        token = await self.token()
        unregistered, _ = create_access_token("admin")
        for bad in ("bad", "a.b.c", token + "x", unregistered):
            self.assertEqual((await self.request("/api/me", headers={"authorization": f"Bearer {bad}"}))[0], 401)

    async def test_disabling_and_reenabling_account_does_not_revive_sessions(self):
        self.add_user("operator", "operator")
        operator = await self.token("operator")
        admin = await self.token()
        for active in (False, True):
            status, _, _ = await self.request("/api/users/operator/active", method="PATCH", data={"is_active": active},
                                            headers={"authorization": f"Bearer {admin}"})
            self.assertEqual(status, 200)
            self.assertEqual((await self.request("/api/me", headers={"authorization": f"Bearer {operator}"}))[0], 401)

    async def test_cookie_mutations_require_same_origin(self):
        token = await self.token()
        for extra in ({}, {"origin": "https://attacker.invalid"}, {"origin": "null"}):
            status, _, _ = await self.request("/api/logout", method="POST", headers={
                "cookie": f"agribank_access={token}", **extra,
            })
            self.assertEqual(status, 403)
        self.assertEqual((await self.request("/api/me", headers={"cookie": f"agribank_access={token}"}))[0], 200)

    async def test_login_rejects_cross_site_requests(self):
        for headers in ({"origin": "https://attacker.invalid"}, {"sec-fetch-site": "cross-site"}):
            self.assertEqual((await self.login(headers=headers))[0], 403)
        self.assertEqual((await self.login(headers={"origin": "http://localhost"}))[0], 200)

    async def test_successful_login_does_not_clear_other_account_failures(self):
        self.add_user("operator", "operator")
        self.assertEqual((await self.login(password="wrong"))[0], 401)
        self.assertEqual((await self.login("operator"))[0], 200)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0], 2)

    async def test_account_limit_is_shared_across_ips_and_case_variants(self):
        for index in range(10):
            self.assertEqual((await self.login("ADMIN" if index % 2 else "admin", password="wrong", ip=f"192.0.2.{index}"))[0], 401)
        self.assertEqual((await self.login(ip="192.0.2.99"))[0], 429)

    async def test_ip_limit_is_shared_across_usernames(self):
        for index in range(20):
            self.assertEqual((await self.login(f"missing-{index}", password="wrong"))[0], 401)
        self.assertEqual((await self.login())[0], 429)

    async def test_password_verification_does_not_hold_write_lock(self):
        original = self.web.verify_password

        def verify_without_lock(password, encoded):
            with closing(sqlite3.connect(self.db_path, timeout=0)) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0], 1)
            return original(password, encoded)

        with patch.object(self.web, "verify_password", side_effect=verify_without_lock):
            self.assertEqual((await self.login())[0], 200)

    async def test_concurrent_logins_reserve_account_quota_atomically(self):
        results = await asyncio.gather(*(
            self.login(password="wrong", ip=f"192.0.2.{index}") for index in range(12)
        ))
        self.assertEqual(sum(status == 401 for status, _, _ in results), 10)
        self.assertEqual(sum(status == 429 for status, _, _ in results), 2)

    async def test_real_json_and_multipart_parsers_return_413_for_chunked_bodies(self):
        for path, content_type, payload in (
            ("/api/login", "application/json", b" " * 65_537),
            ("/api/upload-preview", "multipart/form-data; boundary=test", b"--test\r\nContent-Disposition: form-data; name=\"core_file\"; filename=\"test.csv\"\r\n\r\n" + b"x" * 22_020_097),
        ):
            with self.subTest(path=path):
                self.assertEqual((await self.request(path, method="POST", body=payload,
                                                    headers={"content-type": content_type}))[0], 413)

    async def test_protected_endpoints_and_role_boundaries(self):
        for path in ("/api/me", "/api/users", "/api/stats", "/api/transactions", "/api/runs", "/api/audit-logs", "/api/reports/resolved.csv"):
            self.assertEqual((await self.request(path))[0], 401)
        self.add_user("operator", "operator")
        self.add_user("approver", "approver")
        operator, approver = await self.token("operator"), await self.token("approver")
        self.assertEqual((await self.request("/api/users", headers={"authorization": f"Bearer {operator}"}))[0], 403)
        self.assertEqual((await self.request("/api/restore-baseline", method="POST", headers={"authorization": f"Bearer {operator}"}))[0], 403)
        self.assertEqual((await self.request("/api/resolve", method="POST", data={
            "trans_id": "x", "action_type": "REFUND", "napas_ref_code": "ref", "reason": "reason",
        }, headers={"authorization": f"Bearer {approver}"}))[0], 403)

    async def test_maker_cannot_approve_own_request(self):
        token = await self.token()
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("""INSERT INTO audit_logs
                (trans_id, action_type, napas_ref_code, reason, performed_by, review_status)
                VALUES ('x', 'REFUND', 'ref', 'reason', 'admin', 'PENDING')""")
        self.assertEqual((await self.request("/api/audit-logs/1/decision", method="POST", data={"decision": "APPROVE"},
                                            headers={"authorization": f"Bearer {token}"}))[0], 409)

    async def test_sql_injection_does_not_authenticate_or_change_tables(self):
        self.assertEqual((await self.login("' OR 1=1 --"))[0], 401)
        token = await self.token()
        self.assertEqual((await self.request("/api/anomalies?trans_id=%27%3BDELETE%20FROM%20app_users%3B--",
                                            headers={"authorization": f"Bearer {token}"}))[0], 200)
        self.assertEqual((await self.request("/api/me", headers={"authorization": f"Bearer {token}"}))[0], 200)

    async def test_invalid_date_range_does_not_cause_server_error(self):
        token = await self.token()
        self.assertEqual((await self.request("/api/audit-logs?date_to=9999-12-31",
                                            headers={"authorization": f"Bearer {token}"}))[0], 422)

    async def test_dashboard_keeps_other_runs_visible_and_hides_pending_reviews_including_null_run(self):
        token = await self.token()
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.executemany("""INSERT INTO reconciliation_results
                (trans_id, recon_status, run_id) VALUES (?, ?, ?)""", [
                ("matched", "MATCHED", "current"),
                ("visible", "AMOUNT_MISMATCH", "current"),
                ("pending", "AMOUNT_MISMATCH", "current"),
                ("other-run", "AMOUNT_MISMATCH", "current"),
                ("legacy-pending", "AMOUNT_MISMATCH", None),
                ("legacy-visible", "AMOUNT_MISMATCH", None),
            ])
            connection.executemany("""INSERT INTO audit_logs
                (trans_id, action_type, napas_ref_code, reason, performed_by, review_status, run_id)
                VALUES (?, 'REFUND', 'ref', 'reason', 'admin', 'PENDING', ?)""", [
                ("pending", "current"), ("other-run", "old"),
                ("legacy-pending", None), ("legacy-visible", "old"),
            ])
        headers = {"authorization": f"Bearer {token}"}
        status, _, body = await self.request("/api/stats", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"], {"MATCHED": 1, "AMOUNT_MISMATCH": 3})
        status, _, body = await self.request("/api/anomalies", headers=headers)
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["count"], 3)
        self.assertEqual({row["trans_id"] for row in result["data"]}, {"visible", "other-run", "legacy-visible"})

    async def test_sessions_store_only_hashes_and_survive_schema_initialization(self):
        token = await self.token()
        reconcile_engine.initialize_database()
        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute("SELECT token_hash, username FROM app_sessions").fetchone()
        self.assertEqual(len(row[0]), 64)
        self.assertNotEqual(row[0], token)
        self.assertEqual(row[1], "admin")
        self.assertEqual((await self.request("/api/me", headers={"authorization": f"Bearer {token}"}))[0], 200)

    async def test_upload_preview_reconcile_and_independent_approval(self):
        self.add_user("approver", "approver")
        token = await self.token()
        approver = await self.token("approver")
        fields = []
        for name, payload in (("core_file", "0001,100,SUCCESS"), ("partner_file", "0001,101,SUCCESS")):
            fields.append((f'--test\r\nContent-Disposition: form-data; name="{name}"; filename="../../feed.csv"\r\n'
                           f'Content-Type: text/csv\r\n\r\n TRANS_ID , AMOUNT , STATUS\n{payload}\n\r\n').encode())
        body = b"".join(fields) + b"--test--\r\n"
        headers = {"authorization": f"Bearer {token}", "content-type": "multipart/form-data; boundary=test"}
        status, _, result = await self.request("/api/upload-preview", method="POST", body=body, headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(result)["discrepancy_count"], 1)
        self.assertEqual((await self.request("/api/upload-and-reconcile", method="POST", body=body, headers=headers))[0], 200)
        auth = {"authorization": f"Bearer {token}"}
        status, _, stats_body = await self.request("/api/stats", headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(stats_body)["data"]["AMOUNT_MISMATCH"], 1)
        status, _, anomalies_body = await self.request("/api/anomalies", headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(anomalies_body)["data"][0]["trans_id"], "0001")
        self.assertEqual((await self.request("/api/resolve", method="POST", headers=auth, data={
            "run_id": json.loads(anomalies_body)["data"][0]["run_id"],
            "trans_id": "0001", "action_type": "REFUND", "napas_ref_code": "=1+1", "reason": "Review required",
        }))[0], 200)
        self.assertEqual((await self.request("/api/audit-logs/1/decision", method="POST", data={"decision": "APPROVE"},
                                            headers={"authorization": f"Bearer {approver}"}))[0], 200)
        status, _, report = await self.request("/api/reports/resolved.csv", headers=auth)
        self.assertEqual(status, 200)
        self.assertIn(b"'=1+1", report)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT trans_id, resolved FROM reconciliation_results").fetchone(), ("0001", 1))
            run_id = connection.execute("SELECT run_id FROM reconciliation_runs").fetchone()[0]
        self.assertEqual((config.UPLOAD_DIR / run_id / "core.csv").read_bytes(), b" TRANS_ID , AMOUNT , STATUS\n0001,100,SUCCESS\n")
        self.assertFalse((config.UPLOAD_DIR.parent / "feed.csv").exists())

    async def test_cookies_and_page_security_policy(self):
        with patch.object(config, "COOKIE_SECURE", True):
            status, headers, _ = await self.login()
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers["set-cookie"])
        self.assertIn("Secure", headers["set-cookie"])
        self.assertIn("SameSite=strict", headers["set-cookie"])
        self.assertIn("max-age=", headers["strict-transport-security"])
        status, headers, body = await self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("script-src 'self' https://cdn.jsdelivr.net;", headers["content-security-policy"])
        self.assertNotIn(b"onclick=", body)
        self.assertNotIn(b"<script>", body)
        self.assertEqual(body.count(b'integrity="sha384-'), 4)

    async def upload_pair(self, token, *, partner_amount=101, expected_run_id="", endpoint="/api/upload-and-reconcile"):
        body = b""
        for name, amount in (("core_file", 100), ("partner_file", partner_amount)):
            body += (f'--workflow\r\nContent-Disposition: form-data; name="{name}"; filename="feed.csv"\r\n'
                     f'Content-Type: text/csv\r\n\r\ntrans_id,amount,status\nT-1,{amount},SUCCESS\n\r\n').encode()
        body += (f'--workflow\r\nContent-Disposition: form-data; name="expected_run_id"\r\n\r\n'
                 f'{expected_run_id}\r\n--workflow--\r\n').encode()
        return await self.request(endpoint, method="POST", body=body, headers={
            "authorization": f"Bearer {token}", "content-type": "multipart/form-data; boundary=workflow",
        })

    async def workflow(self, token):
        status, _, body = await self.request("/api/workflow", headers={"authorization": f"Bearer {token}"})
        self.assertEqual(status, 200)
        return json.loads(body)

    async def close_workflow(self, token, run_id):
        return await self.request("/api/close-run", method="POST", data={"run_id": run_id},
                                  headers={"authorization": f"Bearer {token}"})

    async def propose_review(self, token, run_id):
        return await self.request("/api/resolve", method="POST", data={
            "run_id": run_id, "trans_id": "T-1", "action_type": "REFUND",
            "napas_ref_code": "ref", "reason": "Review required",
        }, headers={"authorization": f"Bearer {token}"})

    async def test_only_admin_can_preview_upload_or_close_runs(self):
        for role in ("operator", "approver"):
            self.add_user(role, role)
            token = await self.token(role)
            for endpoint in ("/api/upload-preview", "/api/upload-and-reconcile"):
                self.assertEqual((await self.upload_pair(token, endpoint=endpoint))[0], 403)
            self.assertEqual((await self.close_workflow(token, "missing"))[0], 403)
        token = await self.token()
        self.assertEqual((await self.upload_pair(token, endpoint="/api/upload-preview"))[0], 200)
        self.assertEqual((await self.upload_pair(token))[0], 200)

    async def test_unresolved_and_pending_reviews_block_close_and_replacement(self):
        token = await self.token()
        self.add_user("approver", "approver")
        approver = await self.token("approver")
        self.assertEqual((await self.upload_pair(token))[0], 200)
        state = await self.workflow(token)
        run_id = state["run_id"]
        self.assertEqual((state["unprocessed_count"], state["pending_count"]), (1, 0))
        self.assertEqual((await self.close_workflow(token, run_id))[0], 409)
        self.assertEqual((await self.upload_pair(token, partner_amount=102, expected_run_id=run_id))[0], 409)
        self.assertEqual((await self.propose_review(token, run_id))[0], 200)
        state = await self.workflow(token)
        self.assertEqual((state["unprocessed_count"], state["pending_count"]), (0, 1))
        self.assertFalse(state["can_close"])
        self.assertEqual((await self.close_workflow(token, run_id))[0], 409)
        self.assertEqual((await self.upload_pair(token, partner_amount=102, expected_run_id=run_id))[0], 409)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM reconciliation_runs").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT review_status FROM audit_logs").fetchone()[0], "PENDING")
        status, _, _ = await self.request("/api/audit-logs/1/decision", method="POST",
                                         data={"decision": "REJECT", "comment": "Needs investigation"},
                                         headers={"authorization": f"Bearer {approver}"})
        self.assertEqual(status, 200)
        state = await self.workflow(token)
        self.assertEqual((state["unprocessed_count"], state["pending_count"]), (1, 0))
        self.assertFalse(state["can_close"])

    async def test_independent_approval_then_explicit_close_preserves_history_and_rejects_stale_work(self):
        token = await self.token()
        self.add_user("approver", "approver")
        approver = await self.token("approver")
        self.assertEqual((await self.upload_pair(token))[0], 200)
        first = (await self.workflow(token))["run_id"]
        self.assertEqual((await self.propose_review(token, first))[0], 200)
        self.assertEqual((await self.request("/api/audit-logs/1/decision", method="POST", data={"decision": "APPROVE"},
                                            headers={"authorization": f"Bearer {approver}"}))[0], 200)
        state = await self.workflow(token)
        self.assertTrue(state["can_close"])
        self.assertFalse(state["can_upload"])
        self.assertEqual((await self.upload_pair(token, partner_amount=102, expected_run_id=first))[0], 409)
        self.assertEqual((await self.close_workflow(token, first))[0], 200)
        self.assertEqual((await self.close_workflow(token, first))[0], 200)  # retry is harmless
        self.assertEqual((await self.propose_review(token, first))[0], 409)
        self.assertTrue((await self.workflow(token))["can_upload"])
        self.assertEqual((await self.upload_pair(token, partner_amount=102, expected_run_id=first))[0], 200)
        second = (await self.workflow(token))["run_id"]
        self.assertNotEqual(first, second)
        self.assertEqual((await self.propose_review(token, first))[0], 409)
        self.assertEqual((await self.close_workflow(token, first))[0], 409)
        status, _, body = await self.request(f"/api/runs/{first}", headers={"authorization": f"Bearer {token}"})
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertIsNotNone(result["run"]["closed_at"])
        self.assertEqual(result["run"]["closed_by"], "admin")
        self.assertEqual(result["data"][0]["processing_status"], "APPROVED")
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audit_logs WHERE action_type = 'CLOSE_RUN'").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT resolved FROM reconciliation_results").fetchone()[0], 0)

    async def test_even_matched_only_batch_requires_admin_to_close_before_next_upload(self):
        token = await self.token()
        self.assertEqual((await self.upload_pair(token, partner_amount=100))[0], 200)
        first = (await self.workflow(token))["run_id"]
        self.assertTrue((await self.workflow(token))["can_close"])
        self.assertEqual((await self.upload_pair(token, expected_run_id=first))[0], 409)
        self.assertEqual((await self.close_workflow(token, first))[0], 200)
        # A preview from before another upload cannot overwrite the new run.
        self.assertEqual((await self.upload_pair(token, expected_run_id=""))[0], 409)
        self.assertEqual((await self.upload_pair(token, expected_run_id=first))[0], 200)

    async def test_duplicate_upload_returns_existing_run_without_resetting_approval(self):
        token = await self.token()
        self.add_user("approver", "approver")
        approver = await self.token("approver")
        self.assertEqual((await self.upload_pair(token))[0], 200)
        first = (await self.workflow(token))["run_id"]
        self.assertEqual((await self.propose_review(token, first))[0], 200)
        self.assertEqual((await self.request("/api/audit-logs/1/decision", method="POST", data={"decision": "APPROVE"},
                                            headers={"authorization": f"Bearer {approver}"}))[0], 200)
        status, _, body = await self.upload_pair(token, endpoint="/api/upload-preview")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["duplicate_run_id"], first)
        status, _, body = await self.upload_pair(token, expected_run_id=first)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["detail"]["run_id"], first)
        self.assertEqual((await self.close_workflow(token, first))[0], 200)
        self.assertEqual((await self.upload_pair(token, partner_amount=100, expected_run_id=first))[0], 200)
        second = (await self.workflow(token))["run_id"]
        status, _, body = await self.upload_pair(token, expected_run_id=second)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["detail"]["code"], "duplicate_files")
        self.assertEqual((await self.workflow(token))["run_id"], second)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM reconciliation_runs").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT resolved FROM reconciliation_run_results WHERE run_id = ?", (first,)).fetchone()[0], 1)

    async def test_concurrent_uploads_cannot_replace_an_open_batch(self):
        token = await self.token()
        results = await asyncio.gather(self.upload_pair(token), self.upload_pair(token, partner_amount=102))
        self.assertEqual(sorted(result[0] for result in results), [200, 409])
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM reconciliation_runs").fetchone()[0], 1)

    async def test_restore_endpoint_cannot_bypass_batch_control(self):
        token = await self.token()
        self.assertEqual((await self.request("/api/restore-baseline", method="POST",
                                            headers={"authorization": f"Bearer {token}"}))[0], 410)

    def test_fingerprints_ignore_row_order_but_distinguish_changed_values_and_sources(self):
        rows = [("A", 100, 101, "SUCCESS", "SUCCESS", "AMOUNT_MISMATCH"),
                ("B", 200, None, "SUCCESS", None, "MISSING_IN_PARTNER")]
        self.assertEqual(reconcile_engine.fingerprint_rows(rows), reconcile_engine.fingerprint_rows(list(reversed(rows))))
        changed = [("A", 101, 100, "SUCCESS", "SUCCESS", "AMOUNT_MISMATCH"), rows[1]]
        self.assertNotEqual(reconcile_engine.fingerprint_rows(rows), reconcile_engine.fingerprint_rows(changed))

    async def suspension_scenario(self):
        self.add_user("operator", "operator")
        self.add_user("approver", "approver")
        admin, operator, approver = await self.token(), await self.token("operator"), await self.token("approver")
        run_id = reconcile_engine.replace_reconciliation_results([
            ("matched", 100, 100, "SUCCESS", "SUCCESS", "MATCHED"),
            ("done", 100, 101, "SUCCESS", "SUCCESS", "AMOUNT_MISMATCH"),
            ("pending", 100, 102, "SUCCESS", "SUCCESS", "AMOUNT_MISMATCH"),
            ("unprocessed", 100, 103, "SUCCESS", "SUCCESS", "AMOUNT_MISMATCH"),
        ], performed_by="admin")
        for trans_id in ("done", "pending"):
            status, _, _ = await self.request("/api/resolve", method="POST", data={
                "run_id": run_id, "trans_id": trans_id, "action_type": "REFUND",
                "napas_ref_code": "@reference", "reason": "=Needs investigation",
            }, headers={"authorization": f"Bearer {operator}"})
            self.assertEqual(status, 200)
            if trans_id == "done":
                self.assertEqual((await self.request("/api/audit-logs/1/decision", method="POST",
                    data={"decision": "APPROVE"}, headers={"authorization": f"Bearer {approver}"}))[0], 200)
        return admin, operator, approver, run_id

    async def export_work(self, token, run_id):
        return await self.request("/api/suspensions/export", method="POST", data={"run_id": run_id},
                                  headers={"authorization": f"Bearer {token}"})

    async def acknowledge_work(self, token, run_id, suspension_id, payload):
        return await self.request(f"/api/suspensions/{suspension_id}/acknowledge", method="POST", data={
            "run_id": run_id, "csv_hash": hashlib.sha256(payload).hexdigest(),
        }, headers={"authorization": f"Bearer {token}"})

    async def resume_work(self, token, payload, expected_run_id):
        body = (b'--resume\r\nContent-Disposition: form-data; name="saved_file"; filename="saved.csv"\r\n'
                b'Content-Type: text/csv\r\n\r\n' + payload +
                b'\r\n--resume\r\nContent-Disposition: form-data; name="expected_run_id"\r\n\r\n' +
                expected_run_id.encode() + b'\r\n--resume--\r\n')
        return await self.request("/api/suspensions/resume", method="POST", body=body, headers={
            "authorization": f"Bearer {token}", "content-type": "multipart/form-data; boundary=resume",
        })

    async def test_export_contains_only_outstanding_work_and_unlocks_only_after_complete_download(self):
        admin, _, approver, run_id = await self.suspension_scenario()
        status, headers, payload = await self.export_work(admin, run_id)
        self.assertEqual(status, 200)
        records = list(csv.DictReader(io.StringIO(payload.decode("utf-8-sig"))))
        self.assertEqual(records[0]["record_type"], "MANIFEST")
        self.assertEqual(records[0]["transaction_count"], "2")
        items = {row["trans_id"]: row for row in records[1:]}
        self.assertEqual(set(items), {"pending", "unprocessed"})
        self.assertEqual(items["pending"]["processing_status"], "PENDING")
        self.assertEqual(items["pending"]["pending_log_id"], "2")
        self.assertTrue(items["pending"]["reason"].startswith("'="))
        self.assertTrue(items["pending"]["napas_ref_code"].startswith("'@"))
        self.assertEqual(items["unprocessed"]["processing_status"], "UNPROCESSED")
        state = await self.workflow(admin)
        self.assertTrue(state["suspended"])
        self.assertFalse(state["can_upload"])
        self.assertFalse(state["export_ready"])
        self.assertEqual((await self.upload_pair(admin, expected_run_id=run_id))[0], 409)
        self.assertEqual((await self.request("/api/audit-logs/2/decision", method="POST", data={"decision": "APPROVE"},
                                            headers={"authorization": f"Bearer {approver}"}))[0], 409)
        suspension_id = headers["x-suspension-id"]
        self.assertEqual((await self.acknowledge_work(admin, run_id, suspension_id, payload[:-1]))[0], 409)
        self.assertFalse((await self.workflow(admin))["can_upload"])
        self.assertEqual((await self.acknowledge_work(admin, run_id, suspension_id, payload))[0], 200)
        state = await self.workflow(admin)
        self.assertTrue(state["can_upload"])
        self.assertFalse(state["closed"])
        self.assertTrue(state["export_ready"])

    async def test_urgent_run_then_csv_resume_keeps_pending_identity_and_previously_approved_items(self):
        admin, _, approver, run_id = await self.suspension_scenario()
        status, headers, payload = await self.export_work(admin, run_id)
        self.assertEqual(status, 200)
        self.assertEqual((await self.acknowledge_work(admin, run_id, headers["x-suspension-id"], payload))[0], 200)
        self.assertEqual((await self.upload_pair(admin, expected_run_id=run_id))[0], 200)
        urgent_id = (await self.workflow(admin))["run_id"]
        self.assertEqual((await self.workflow(admin))["pending_count"], 0)
        # The unfinished urgent run cannot be overwritten by an old CSV.
        self.assertEqual((await self.resume_work(admin, payload, urgent_id))[0], 409)
        self.assertEqual((await self.propose_review(admin, urgent_id))[0], 200)
        with closing(sqlite3.connect(self.db_path)) as connection:
            urgent_log = connection.execute("SELECT log_id FROM audit_logs WHERE run_id = ? AND review_status = 'PENDING'", (urgent_id,)).fetchone()[0]
        self.assertEqual((await self.request(f"/api/audit-logs/{urgent_log}/decision", method="POST", data={"decision": "APPROVE"},
                                            headers={"authorization": f"Bearer {approver}"}))[0], 200)
        self.assertEqual((await self.close_workflow(admin, urgent_id))[0], 200)
        self.assertEqual((await self.resume_work(admin, payload, run_id))[0], 409)  # stale page
        status, _, result = await self.resume_work(admin, payload, urgent_id)
        self.assertEqual(status, 200)
        state = json.loads(result)
        self.assertEqual(state["run_id"], run_id)
        self.assertFalse(state["suspended"])
        self.assertEqual((state["unprocessed_count"], state["pending_count"]), (1, 1))
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT resolved FROM reconciliation_results WHERE trans_id = 'done'").fetchone()[0], 1)
            pending = connection.execute("SELECT log_id, performed_by, reason FROM audit_logs WHERE run_id = ? AND review_status = 'PENDING'", (run_id,)).fetchall()
            self.assertEqual(pending, [(2, "operator", "=Needs investigation")])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM reconciliation_runs").fetchone()[0], 2)
        self.assertEqual((await self.request("/api/audit-logs/2/decision", method="POST", data={"decision": "APPROVE"},
                                            headers={"authorization": f"Bearer {approver}"}))[0], 200)
        self.assertEqual((await self.resume_work(admin, payload, run_id))[0], 409)  # consumed file

    async def test_csv_tampering_and_wrong_format_cannot_change_work_or_unlock_upload(self):
        admin, _, _, run_id = await self.suspension_scenario()
        _, _, payload = await self.export_work(admin, run_id)
        self.assertEqual((await self.resume_work(admin, payload.replace(b"103", b"999"), run_id))[0], 409)
        self.assertEqual((await self.resume_work(admin, b"trans_id,amount,status\nx,1,SUCCESS\n", run_id))[0], 422)
        state = await self.workflow(admin)
        self.assertTrue(state["suspended"])
        self.assertFalse(state["can_upload"])
        self.assertEqual((state["unprocessed_count"], state["pending_count"]), (1, 1))

    async def test_failed_download_can_be_retried_and_lost_csv_can_be_downloaded_again(self):
        admin, _, _, run_id = await self.suspension_scenario()
        _, headers, payload = await self.export_work(admin, run_id)
        status, retry_headers, retry_payload = await self.export_work(admin, run_id)
        self.assertEqual(status, 200)
        self.assertEqual(headers["x-suspension-id"], retry_headers["x-suspension-id"])
        self.assertEqual(payload, retry_payload)
        status, _, listing = await self.request("/api/suspensions", headers={"authorization": f"Bearer {admin}"})
        self.assertEqual(status, 200)
        saved = json.loads(listing)["data"]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["status"], "PREPARED")
        status, _, downloaded = await self.request(f'/api/suspensions/{saved[0]["suspension_id"]}.csv',
                                                   headers={"authorization": f"Bearer {admin}"})
        self.assertEqual(status, 200)
        self.assertEqual(payload, downloaded)
        # A complete file also lets admin undo a prepared pause without an urgent upload.
        self.assertEqual((await self.resume_work(admin, downloaded, run_id))[0], 200)
        self.assertFalse((await self.workflow(admin))["suspended"])

    async def test_pause_resume_permissions_and_concurrent_resume_are_enforced(self):
        admin, operator, approver, run_id = await self.suspension_scenario()
        _, headers, payload = await self.export_work(admin, run_id)
        suspension_id = headers["x-suspension-id"]
        for token in (operator, approver):
            self.assertEqual((await self.export_work(token, run_id))[0], 403)
            self.assertEqual((await self.acknowledge_work(token, run_id, suspension_id, payload))[0], 403)
            self.assertEqual((await self.resume_work(token, payload, run_id))[0], 403)
            for path in ("/api/suspensions", f"/api/suspensions/{suspension_id}.csv"):
                self.assertEqual((await self.request(path, headers={"authorization": f"Bearer {token}"}))[0], 403)
        results = await asyncio.gather(self.resume_work(admin, payload, run_id), self.resume_work(admin, payload, run_id))
        self.assertEqual(sorted(result[0] for result in results), [200, 409])
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audit_logs WHERE action_type = 'RESUME_RUN'").fetchone()[0], 1)

    async def test_failed_export_rolls_back_pause_and_saved_rows(self):
        admin, _, _, run_id = await self.suspension_scenario()
        with patch.object(suspensions, "MAX_CSV_BYTES", 10):
            self.assertEqual((await self.export_work(admin, run_id))[0], 409)
        state = await self.workflow(admin)
        self.assertFalse(state["suspended"])
        self.assertEqual((state["unprocessed_count"], state["pending_count"]), (1, 1))
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM reconciliation_suspensions").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM reconciliation_suspension_items").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audit_logs WHERE action_type = 'SUSPEND_RUN'").fetchone()[0], 0)

    async def test_saved_work_survives_reinitialization_and_jwt_secret_rotation(self):
        admin, _, _, run_id = await self.suspension_scenario()
        _, _, payload = await self.export_work(admin, run_id)
        reconcile_engine.initialize_database()
        self.assertTrue((await self.workflow(admin))["suspended"])
        with patch.object(config, "JWT_SECRET", "rotated-test-secret-only-" * 4):
            new_admin = await self.token()
            self.assertEqual((await self.resume_work(new_admin, payload, run_id))[0], 200)
            self.assertEqual((await self.workflow(new_admin))["pending_count"], 1)

    def test_csv_preserves_ids_with_normalized_headers(self):
        frame = self.web.parse_uploaded_csv("test.csv", b" TRANS_ID , AMOUNT , STATUS ,extra\n0000123,100,SUCCESS,ignored\n", "Core")
        self.assertEqual(frame["trans_id"].tolist(), ["0000123"])
        self.assertEqual(frame["amount"].tolist(), [100])

    def test_csv_rejects_ragged_rows_duplicate_headers_and_nul(self):
        for body in (
            b"trans_id,amount,status\nx,100,SUCCESS,extra\n",
            b"trans_id,amount,status\nx,100\n",
            b"trans_id,amount,status, TRANS_ID\nx,100,SUCCESS,y\n",
            b"trans_id,amount,status\nx\x00y,100,SUCCESS\n",
            b'trans_id,amount,status\n"unterminated,100,SUCCESS\n',
            b"trans_id,amount,status\nx,100,SUCCESS\nx,100,SUCCESS\n",
            b"trans_id,amount,status\nx,-1,SUCCESS\n",
        ):
            with self.subTest(body=body), self.assertRaises(HTTPException) as error:
                self.web.parse_uploaded_csv("test.csv", body, "Core")
            self.assertEqual(error.exception.status_code, 422)

    def test_formula_prefixes_are_neutralized_in_reports(self):
        for value in ("=1+1", "+1+1", "-1+1", "@SUM(A1)", " \t=1", "\r\n=1"):
            with self.subTest(value=value):
                self.assertTrue(self.web.csv_safe_text(value).startswith("'"))

    def test_tokens_expire_and_malformed_tokens_fail_closed(self):
        with patch("agribank_recon.security.time.time", return_value=1000):
            token, _ = create_access_token("admin")
        with patch("agribank_recon.security.time.time", return_value=1000 + config.ACCESS_TOKEN_TTL_MINUTES * 60):
            self.assertIsNone(verify_access_token(token))
        for token in ("", "a" * 4097, "☃.x.x", "a.b.!", "a.b.c.d"):
            self.assertIsNone(verify_access_token(token))


if __name__ == "__main__":
    unittest.main()
