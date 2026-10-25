"""Security regressions against the real ASGI application and isolated databases."""

import asyncio
import importlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi import HTTPException

from agribank_recon import config, reconcile_engine
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
