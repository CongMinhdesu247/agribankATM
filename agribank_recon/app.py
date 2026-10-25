from pathlib import Path
from datetime import date, datetime, timedelta
import csv
import hashlib
import hmac
import io
import logging
import os
import shutil
import tempfile
import time
import uuid
from typing import Literal
import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile, File, Form
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from . import config, reconcile_engine
from .database import connect_database, get_table_columns, is_integrity_error
from .request_limits import RequestBodyLimitMiddleware
from .security import create_access_token, hash_password, verify_access_token, verify_password

logger = logging.getLogger(__name__)

if len(config.JWT_SECRET) < 32 or not config.APP_USERNAME or not config.APP_PASSWORD_HASH:
    raise RuntimeError("Authentication is not configured. Run python -m scripts.setup_admin to create the .env file.")

app = FastAPI(title="Agribank Reconciliation Review", docs_url=None, redoc_url=None, openapi_url=None)
BASE_DIR = config.BASE_DIR
DB_PATH = config.DB_PATH
reconcile_engine.initialize_database()

def seed_admin_user() -> None:
    """Import the configured bootstrap admin once, then use database users."""
    if not config.APP_USERNAME or not config.APP_PASSWORD_HASH:
        return
    conn = get_db_connection()
    try:
        conn.execute("""
            INSERT OR IGNORE INTO app_users (username, password_hash, role, is_active, created_at, created_by)
            VALUES (?, ?, 'admin', 1, CURRENT_TIMESTAMP, 'bootstrap')
        """, (config.APP_USERNAME, config.APP_PASSWORD_HASH))
        conn.commit()
    finally:
        conn.close()


@app.middleware("http")
async def set_security_headers(request: Request, call_next):
    request_id = uuid.uuid4().hex
    request.state.request_id = request_id
    started_at = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("Request failed request_id=%s", request_id)
        raise
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "font-src 'self' https://cdn.jsdelivr.net; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    )
    if config.COOKIE_SECURE or request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    if request.url.path.startswith(("/api/", "/health/")):
        response.headers["Cache-Control"] = "no-store"
    if not request.url.path.startswith("/health/"):
        logger.info(
            "http_request request_id=%s method=%s status=%s elapsed_ms=%.1f",
            request_id, request.method, response.status_code, (time.perf_counter() - started_at) * 1000,
        )
    return response


app.add_middleware(
    RequestBodyLimitMiddleware,
    max_upload_body_bytes=(2 * config.MAX_UPLOAD_BYTES) + 1024 * 1024,
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(config.ALLOWED_HOSTS))

class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=256)


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=50, pattern=r"^[A-Za-z0-9._-]+$")
    password: str = Field(min_length=12, max_length=256)
    role: Literal["admin", "operator", "approver"]


class UserActiveRequest(BaseModel):
    is_active: bool


class AuditDecisionRequest(BaseModel):
    decision: Literal["APPROVE", "REJECT"]
    comment: str = Field(default="", max_length=2000)

bearer_scheme = HTTPBearer(auto_error=False)


def require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin", "").rstrip("/")
    expected_origin = f"{request.url.scheme}://{request.url.netloc}"
    # Non-browser API clients can omit Origin; browsers must provide the real
    # origin for unsafe cookie requests. Fetch Metadata also blocks hostile sites.
    if (origin and origin != expected_origin) or request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")


def current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
):
    if credentials is not None and credentials.scheme.lower() == "bearer":
        token = credentials.credentials
    else:
        token = request.cookies.get("agribank_access")
        if token:
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                require_same_origin(request)
                if not request.headers.get("origin"):
                    raise HTTPException(status_code=403, detail="Cross-origin request rejected")
    username = verify_access_token(token) if token else None
    if username is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
    conn = get_db_connection()
    try:
        user = conn.execute(
            """SELECT u.username, u.role, u.is_active FROM app_users u
               JOIN app_sessions s ON s.username = u.username
               WHERE LOWER(u.username) = LOWER(?) AND s.token_hash = ? AND s.expires_at > ?""",
            (username, hashlib.sha256(token.encode("ascii")).hexdigest(), time.time()),
        ).fetchone()
    finally:
        conn.close()
    if user is None or not user["is_active"]:
        raise HTTPException(status_code=401, detail="Invalid or disabled account", headers={"WWW-Authenticate": "Bearer"})
    request.state.token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    return {"username": user["username"], "role": user["role"]}


@app.get("/api/me")
def get_current_user(user=Depends(current_user)):
    return user


def require_role(*allowed_roles: str):
    def dependency(user=Depends(current_user)):
        if user["role"] not in allowed_roles:
            raise HTTPException(status_code=403, detail="You do not have permission to perform this action")
        return user
    return dependency


@app.get("/api/users")
def list_users(_user=Depends(require_role("admin"))):
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT username, role, is_active, created_at, created_by FROM app_users ORDER BY LOWER(username)"
        ).fetchall()
    finally:
        conn.close()
    return {"data": [dict(row) for row in rows]}


@app.post("/api/users", status_code=201)
def create_user(req: UserCreateRequest, user=Depends(require_role("admin"))):
    conn = get_db_connection()
    try:
        conn.execute("""
            INSERT INTO app_users (username, password_hash, role, is_active, created_at, created_by)
            VALUES (?, ?, ?, 1, ?, ?)
        """, (req.username, hash_password(req.password), req.role,
              datetime.now().astimezone().isoformat(timespec="seconds"), user["username"]))
        conn.commit()
    except Exception as error:
        if is_integrity_error(error):
            raise HTTPException(status_code=409, detail="Username already exists") from error
        raise
    finally:
        conn.close()
    return {"status": "success", "username": req.username, "role": req.role}


@app.patch("/api/users/{username}/active")
def set_user_active(username: str, req: UserActiveRequest, user=Depends(require_role("admin"))):
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        target = conn.execute(
            "SELECT username, role, is_active FROM app_users WHERE LOWER(username) = LOWER(?)", (username,)
        ).fetchone()
        if target is None:
            raise HTTPException(status_code=404, detail="User not found")
        if not req.is_active and target["username"].casefold() == user["username"].casefold():
            raise HTTPException(status_code=409, detail="You cannot disable your own account")
        if not req.is_active and target["role"] == "admin" and target["is_active"]:
            active_admins = conn.execute(
                "SELECT COUNT(*) FROM app_users WHERE role = 'admin' AND is_active = 1"
            ).fetchone()[0]
            if active_admins <= 1:
                raise HTTPException(status_code=409, detail="At least one active administrator is required")
        conn.execute("UPDATE app_users SET is_active = ? WHERE LOWER(username) = LOWER(?)",
                     (int(req.is_active), username))
        if not req.is_active:
            conn.execute("DELETE FROM app_sessions WHERE username = ?", (target["username"],))
        conn.commit()
    finally:
        conn.close()
    return {"status": "success", "username": target["username"], "is_active": req.is_active}

@app.post("/api/login")
def login(req: LoginRequest, request: Request, response: Response):
    require_same_origin(request)
    now = time.time()
    client_ip = request.client.host if request.client else "unknown"
    ip_hash = hmac.new(config.JWT_SECRET.encode(), f"login-ip:{client_ip}".encode("utf-8"), hashlib.sha256).hexdigest()
    username = req.username.strip().casefold()
    username_hash = hmac.new(
        config.JWT_SECRET.encode(), f"login-user:{username}".encode("utf-8"), hashlib.sha256
    ).hexdigest()
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (now - 900,))
        ip_attempts = conn.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE ip_hash = ? AND attempted_at >= ?",
            (ip_hash, now - 300),
        ).fetchone()[0]
        account_attempts = conn.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE username_hash = ? AND attempted_at >= ?",
            (username_hash, now - 900),
        ).fetchone()[0]
        if ip_attempts >= 20 or account_attempts >= 10:
            conn.commit()
            logger.warning("login_rejected request_id=%s reason=throttled", request.state.request_id)
            raise HTTPException(status_code=429, detail="Too many login attempts; try again later")
        # Reserve the attempt before expensive password hashing, so concurrent
        # requests cannot bypass the quota. Successful logins do not reset it.
        conn.execute(
            "INSERT INTO login_attempts (ip_hash, username_hash, attempted_at) VALUES (?, ?, ?)",
            (ip_hash, username_hash, now),
        )
        conn.commit()
        user = conn.execute(
            "SELECT username, password_hash, role, is_active FROM app_users WHERE LOWER(username) = LOWER(?)",
            (req.username.strip(),),
        ).fetchone()
    finally:
        conn.close()
    # Do not hold the global writer/advisory lock while running scrypt.
    password_ok = verify_password(req.password, user["password_hash"] if user else config.APP_PASSWORD_HASH)
    if user is None or not user["is_active"] or not password_ok:
        logger.warning("login_rejected request_id=%s reason=invalid_credentials", request.state.request_id)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token, expires_in = create_access_token(user["username"])
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        fresh_user = conn.execute(
            "SELECT password_hash, is_active, role FROM app_users WHERE username = ?", (user["username"],)
        ).fetchone()
        if (fresh_user is None or not fresh_user["is_active"]
                or fresh_user["password_hash"] != user["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid username or password")
        conn.execute("DELETE FROM app_sessions WHERE expires_at <= ?", (time.time(),))
        conn.execute(
            "INSERT INTO app_sessions (token_hash, username, expires_at) VALUES (?, ?, ?)",
            (hashlib.sha256(token.encode("ascii")).hexdigest(), user["username"], now + expires_in),
        )
        conn.commit()
    finally:
        conn.close()
    response.set_cookie(
        key="agribank_access", value=token, max_age=expires_in, httponly=True,
        secure=config.COOKIE_SECURE or request.url.scheme == "https", samesite="strict", path="/",
    )
    logger.info("login_succeeded request_id=%s", request.state.request_id)
    return {"token_type": "cookie", "expires_in": expires_in,
            "username": user["username"], "role": fresh_user["role"]}


@app.post("/api/logout")
def logout(request: Request, response: Response, _user=Depends(current_user)):
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM app_sessions WHERE token_hash = ?", (request.state.token_hash,))
        conn.commit()
    finally:
        conn.close()
    response.delete_cookie(
        key="agribank_access", path="/", secure=config.COOKIE_SECURE or request.url.scheme == "https",
        httponly=True, samesite="strict",
    )
    logger.info("logout_succeeded request_id=%s", request.state.request_id)
    return {"status": "ok"}

# Serve static assets from the project directory.
app.mount("/static", StaticFiles(directory=config.APP_DIR / "static"), name="static")

# 3. Trả về giao diện web trực tiếp từ Server
@app.get("/")
def read_root():
    return FileResponse(
        config.APP_DIR / "templates" / "index.html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )

def get_db_connection():
    return connect_database(DB_PATH)


seed_admin_user()


@app.get("/health/live")
def health_live():
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready():
    conn = get_db_connection()
    try:
        conn.execute("SELECT 1").fetchone()
    except Exception as error:
        logger.exception("Readiness check failed")
        raise HTTPException(status_code=503, detail="Database is not ready") from error
    finally:
        conn.close()
    return {"status": "ready"}

def ensure_audit_report_columns(conn):
    columns = get_table_columns(conn, "audit_logs")
    snapshot_columns = {
        "core_amount": "REAL",
        "partner_amount": "REAL",
        "core_status": "TEXT",
        "partner_status": "TEXT",
        "recon_status": "TEXT",
    }
    for name, column_type in snapshot_columns.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE audit_logs ADD COLUMN {name} {column_type}")

def csv_safe_text(value):
    text = "" if value is None else str(value).replace("\r", " ").replace("\n", " ")
    if text.lstrip(" \t").startswith(("=", "+", "-", "@")):
        return "'" + text
    return text

# --- CÁC API KHÁC GIỮ NGUYÊN ---
@app.get("/api/stats")
def get_stats(_user=Depends(current_user)):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT recon_status, COUNT(*) as total
            FROM reconciliation_results
            WHERE resolved = 0 AND recon_status != 'MATCHED'
              AND NOT EXISTS (
                  SELECT 1 FROM audit_logs a WHERE a.trans_id = reconciliation_results.trans_id
                    AND a.run_id IS reconciliation_results.run_id AND a.review_status = 'PENDING'
              )
            GROUP BY recon_status
        ''')
        rows = cursor.fetchall()
        stats = {row['recon_status']: row['total'] for row in rows}
        cursor.execute("SELECT COUNT(*) AS total FROM reconciliation_results WHERE recon_status = 'MATCHED'")
        stats['MATCHED'] = cursor.fetchone()['total']
    finally:
        conn.close()
    return {"status": "success", "data": stats}


def _bounded_page(page: int, page_size: int, maximum_page_size: int) -> tuple[int, int]:
    # Keep SQLite LIMIT/OFFSET bind values in a safe range even for hostile
    # integer query parameters that are far beyond SQLite's signed 64-bit limit.
    return min(1_000_000, max(1, page)), min(maximum_page_size, max(1, page_size))


@app.get("/api/runs")
def list_reconciliation_runs(page: int = 1, page_size: int = 25, _user=Depends(current_user)):
    page, page_size = _bounded_page(page, page_size, 100)
    conn = get_db_connection()
    try:
        total = conn.execute("SELECT COUNT(*) FROM reconciliation_runs").fetchone()[0]
        rows = conn.execute("""
            SELECT run_id, created_at, performed_by, core_filename, partner_filename,
                   transaction_count, matched_count, discrepancy_count
            FROM reconciliation_runs ORDER BY created_at DESC, run_id DESC LIMIT ? OFFSET ?
        """, (page_size, (page - 1) * page_size)).fetchall()
    finally:
        conn.close()
    return {"count": total, "page": page, "page_size": page_size, "data": [dict(row) for row in rows]}


@app.get("/api/runs/{run_id}")
def get_reconciliation_run(run_id: str, page: int = 1, page_size: int = 100, _user=Depends(current_user)):
    page, page_size = _bounded_page(page, page_size, 200)
    conn = get_db_connection()
    try:
        run = conn.execute("SELECT * FROM reconciliation_runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            raise HTTPException(status_code=404, detail="Reconciliation run not found")
        rows = conn.execute("""
            SELECT trans_id, core_amount, partner_amount, core_status, partner_status, recon_status
            FROM reconciliation_run_results WHERE run_id = ? ORDER BY trans_id LIMIT ? OFFSET ?
        """, (run_id, page_size, (page - 1) * page_size)).fetchall()
    finally:
        conn.close()
    return {"run": dict(run), "page": page, "page_size": page_size, "data": [dict(row) for row in rows]}

@app.get("/api/anomalies")
def get_anomalies(
    page: int = 1, page_size: int = 100, trans_id: str = "", recon_status: str = "",
    core_status: str = "", partner_status: str = "", _user=Depends(current_user),
):
    page, page_size = _bounded_page(page, page_size, 200)
    clauses = ["recon_status != 'MATCHED'", "resolved = 0",
               "NOT EXISTS (SELECT 1 FROM audit_logs a WHERE a.trans_id = reconciliation_results.trans_id "
               "AND a.run_id IS reconciliation_results.run_id AND a.review_status = 'PENDING')"]
    params: list[object] = []
    if trans_id.strip():
        clauses.append("trans_id LIKE ? ESCAPE '\\'")
        escaped = trans_id.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")
    for column, value in (("recon_status", recon_status), ("core_status", core_status), ("partner_status", partner_status)):
        if value:
            if value == "__MISSING__" and column != "recon_status":
                clauses.append(f"{column} IS NULL")
            else:
                clauses.append(f"{column} = ?")
                params.append(value)
    where = " AND ".join(clauses)
    conn = get_db_connection()
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM reconciliation_results WHERE {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM reconciliation_results WHERE {where} ORDER BY trans_id ASC LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
    finally:
        conn.close()
    return {"status": "success", "count": total, "page": page, "page_size": page_size,
            "data": [dict(row) for row in rows]}


@app.get("/api/anomaly-options")
def get_anomaly_options(_user=Depends(current_user)):
    conn = get_db_connection()
    try:
        core = conn.execute("""
            SELECT DISTINCT core_status FROM reconciliation_results
            WHERE resolved = 0 AND core_status IS NOT NULL ORDER BY core_status
        """).fetchall()
        partner = conn.execute("""
            SELECT DISTINCT partner_status FROM reconciliation_results
            WHERE resolved = 0 AND partner_status IS NOT NULL ORDER BY partner_status
        """).fetchall()
    finally:
        conn.close()
    return {"core_statuses": [row[0] for row in core], "partner_statuses": [row[0] for row in partner]}

@app.get("/api/transactions")
def get_transactions(page: int = 1, page_size: int = 100, _user=Depends(current_user)):
    page, page_size = _bounded_page(page, page_size, 200)
    conn = get_db_connection()
    try:
        total = conn.execute("SELECT COUNT(*) FROM reconciliation_results").fetchone()[0]
        rows = conn.execute("SELECT * FROM reconciliation_results ORDER BY trans_id ASC LIMIT ? OFFSET ?",
                            (page_size, (page - 1) * page_size)).fetchall()
    finally:
        conn.close()
    return {"status": "success", "count": total, "page": page, "page_size": page_size,
            "data": [dict(row) for row in rows]}

@app.post("/api/restore-baseline")
def restore_baseline(user=Depends(require_role("admin"))):
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        state = cursor.execute("SELECT 1 FROM reconciliation_baseline_state WHERE baseline_id = 1").fetchone()
        if state is None:
            legacy_rows = cursor.execute("SELECT * FROM reconciliation_baseline").fetchall()
            cursor.execute("""
                INSERT INTO reconciliation_baseline_state (baseline_id, captured_at, transaction_count)
                VALUES (1, CURRENT_TIMESTAMP, ?)
            """, (len(legacy_rows),))
            if legacy_rows:
                cursor.executemany("""
                    INSERT INTO reconciliation_baseline_results
                        (trans_id, core_amount, partner_amount, core_status, partner_status,
                         recon_status, resolved, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, [tuple(row[key] for key in ("trans_id", "core_amount", "partner_amount", "core_status",
                                                       "partner_status", "recon_status", "resolved", "created_at"))
                      for row in legacy_rows])
        cursor.execute("""
            UPDATE audit_logs SET review_status = 'SUPERSEDED', approval_comment = 'Baseline restored'
            WHERE review_status = 'PENDING'
        """)
        cursor.execute("DELETE FROM reconciliation_results")
        cursor.execute('''
            INSERT INTO reconciliation_results
                (trans_id, core_amount, partner_amount, core_status, partner_status, recon_status,
                 resolved, created_at, run_id)
            SELECT trans_id, core_amount, partner_amount, core_status, partner_status, recon_status,
                   resolved, created_at, run_id
            FROM reconciliation_baseline_results
        ''')
        count = cursor.execute("SELECT COUNT(*) FROM reconciliation_results").fetchone()[0]
        cursor.execute("""
            INSERT INTO audit_logs (trans_id, action_type, napas_ref_code, reason, performed_by,
                                    review_status, approved_by, approved_at)
            VALUES ('*', 'RESTORE_BASELINE', '-', 'Restored the initial reconciliation dataset', ?,
                    'APPROVED', ?, CURRENT_TIMESTAMP)
        """, (user["username"], user["username"]))
        conn.commit()
        return {"status": "success", "count": count, "message": "Đã khôi phục danh sách ban đầu"}
    except Exception as error:
        conn.rollback()
        if isinstance(error, HTTPException):
            raise
        logger.exception("Baseline restore failed")
        raise HTTPException(status_code=500, detail="Could not restore the initial dataset") from error
    finally:
        conn.close()

class ResolveRequest(BaseModel):
    trans_id: str = Field(min_length=1, max_length=100)
    action_type: Literal["FORCE_SUCCESS", "REFUND"]
    napas_ref_code: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)

@app.post("/api/resolve")
def resolve_anomaly(req: ResolveRequest, user=Depends(require_role("admin", "operator"))):
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ensure_audit_report_columns(conn)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM reconciliation_results WHERE trans_id = ?", (req.trans_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Transaction not found")
        if row["recon_status"] == "MATCHED" or row["resolved"]:
            raise HTTPException(status_code=409, detail="Transaction is not pending review")
        if not req.napas_ref_code.strip() or not req.reason.strip():
            raise HTTPException(status_code=422, detail="Reference and reason are required")

        pending = cursor.execute("""
            SELECT 1 FROM audit_logs WHERE trans_id = ? AND run_id IS ? AND review_status = 'PENDING'
        """, (req.trans_id, row["run_id"])).fetchone()
        if pending:
            raise HTTPException(status_code=409, detail="This transaction is already waiting for approval")
        cursor.execute('''
            INSERT INTO audit_logs
                (trans_id, action_type, napas_ref_code, reason, performed_by,
                 core_amount, partner_amount, core_status, partner_status, recon_status, run_id, review_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
        ''', (
            req.trans_id, req.action_type, req.napas_ref_code.strip(), req.reason.strip(), user["username"],
            row["core_amount"], row["partner_amount"], row["core_status"], row["partner_status"], row["recon_status"],
            row["run_id"],
        ))
        conn.commit()
        return {"status": "success", "message": "Review submitted for approval"}
    except HTTPException:
        conn.rollback()
        raise
    except Exception as error:
        conn.rollback()
        logger.exception("Anomaly review submission failed")
        raise HTTPException(status_code=500, detail="Could not submit the review request") from error
    finally:
        conn.close()


@app.post("/api/audit-logs/{log_id}/decision")
def decide_audit_log(log_id: int, req: AuditDecisionRequest, user=Depends(require_role("admin", "approver"))):
    if req.decision == "REJECT" and not req.comment.strip():
        raise HTTPException(status_code=422, detail="A rejection reason is required")
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        audit = conn.execute("SELECT * FROM audit_logs WHERE log_id = ?", (log_id,)).fetchone()
        if audit is None:
            raise HTTPException(status_code=404, detail="Review request not found")
        if audit["review_status"] != "PENDING":
            raise HTTPException(status_code=409, detail="Review request has already been decided")
        if audit["performed_by"].casefold() == user["username"].casefold():
            raise HTTPException(status_code=409, detail="The person who submitted a review cannot approve it")
        decision = "APPROVED" if req.decision == "APPROVE" else "REJECTED"
        conn.execute("""
            UPDATE audit_logs SET review_status = ?, approved_by = ?, approved_at = CURRENT_TIMESTAMP,
                   approval_comment = ?
            WHERE log_id = ? AND review_status = 'PENDING'
        """, (decision, user["username"], req.comment.strip(), log_id))
        if req.decision == "APPROVE":
            updated = conn.execute("""
                UPDATE reconciliation_results SET resolved = 1
                WHERE trans_id = ? AND run_id IS ? AND resolved = 0
            """, (audit["trans_id"], audit["run_id"]))
            if updated.rowcount != 1:
                raise HTTPException(status_code=409, detail="Transaction is no longer awaiting resolution")
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception as error:
        conn.rollback()
        logger.exception("Audit review decision failed")
        raise HTTPException(status_code=500, detail="Could not save the review decision") from error
    finally:
        conn.close()
    return {"status": "success", "review_status": decision}

@app.get("/api/reports/resolved.csv")
def export_resolved_report(_user=Depends(current_user)):
    conn = get_db_connection()
    try:
        ensure_audit_report_columns(conn)
        conn.commit()
        rows = conn.execute('''
            SELECT a.log_id, a.trans_id,
                   COALESCE(a.core_amount, r.core_amount) AS core_amount,
                   COALESCE(a.partner_amount, r.partner_amount) AS partner_amount,
                   COALESCE(a.core_status, r.core_status) AS core_status,
                   COALESCE(a.partner_status, r.partner_status) AS partner_status,
                   COALESCE(a.recon_status, r.recon_status) AS recon_status,
                   a.action_type, a.napas_ref_code, a.reason, a.performed_by, a.created_at, a.run_id,
                   a.review_status, a.approved_by, a.approved_at, a.approval_comment
            FROM audit_logs AS a
            LEFT JOIN reconciliation_results AS r
                ON r.trans_id = a.trans_id AND r.run_id = a.run_id
            ORDER BY a.created_at ASC, a.log_id ASC
        ''')
    except Exception:
        conn.close()
        raise

    headers = [
            "Mã log", "Mã giao dịch", "Tiền Core", "Tiền Napas", "Trạng thái Core",
            "Trạng thái Napas", "Kết quả đối soát", "Hình thức xử lý", "Mã tham chiếu Napas",
            "Lý do/Ghi chú", "Nhân viên đề xuất", "Thời gian đề xuất", "Mã lần đối soát",
            "Trạng thái duyệt", "Người duyệt", "Thời gian duyệt", "Ghi chú phê duyệt",
    ]

    def stream_report():
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        try:
            writer.writerow(headers)
            yield "\ufeff" + buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
            for row in rows:
                writer.writerow([
                    row["log_id"], csv_safe_text(row["trans_id"]), row["core_amount"], row["partner_amount"],
                    csv_safe_text(row["core_status"]), csv_safe_text(row["partner_status"]),
                    csv_safe_text(row["recon_status"]), csv_safe_text(row["action_type"]),
                    csv_safe_text(row["napas_ref_code"]), csv_safe_text(row["reason"]),
                    csv_safe_text(row["performed_by"]), csv_safe_text(row["created_at"]), csv_safe_text(row["run_id"]),
                    csv_safe_text(row["review_status"]), csv_safe_text(row["approved_by"]),
                    csv_safe_text(row["approved_at"]), csv_safe_text(row["approval_comment"]),
                ])
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate(0)
        finally:
            conn.close()

    filename = f"bao_cao_tra_soat_{datetime.now():%Y%m%d_%H%M%S}.csv"
    return StreamingResponse(
            stream_report(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
@app.get("/api/audit-logs")
def get_audit_logs(
    page: int = 1, page_size: int = 50, review_status: str = "", performed_by: str = "",
    date_from: str = "", date_to: str = "", _user=Depends(current_user),
):
    page, page_size = _bounded_page(page, page_size, 200)
    clauses, params = [], []
    if review_status:
        clauses.append("review_status = ?")
        params.append(review_status)
    if performed_by.strip():
        clauses.append("performed_by LIKE ?")
        params.append(f"%{performed_by.strip()}%")
    for value, operator in ((date_from, ">="), (date_to, "<")):
        if value:
            try:
                parsed = date.fromisoformat(value)
                bound = parsed.isoformat() if operator == ">=" else (parsed + timedelta(days=1)).isoformat()
            except (ValueError, OverflowError) as error:
                raise HTTPException(status_code=422, detail="Dates must use YYYY-MM-DD format") from error
            clauses.append(f"created_at {operator} ?")
            params.append(bound)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = get_db_connection()
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM audit_logs{where}", params).fetchone()[0]
        rows = conn.execute(f"SELECT * FROM audit_logs{where} ORDER BY created_at DESC, log_id DESC LIMIT ? OFFSET ?",
                            [*params, page_size, (page - 1) * page_size]).fetchall()
    finally:
        conn.close()
    return {"status": "success", "count": total, "page": page, "page_size": page_size,
            "data": [dict(row) for row in rows]}

def parse_uploaded_csv(filename: str | None, payload: bytes, label: str) -> pd.DataFrame:
    if not filename or Path(filename).suffix.lower() != ".csv":
        raise HTTPException(status_code=415, detail=f"{label} file must have a .csv extension")
    if not payload:
        raise HTTPException(status_code=422, detail=f"{label} CSV is empty")
    if len(payload) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"{label} CSV exceeds the 10 MB limit")

    try:
        reader = csv.reader(io.StringIO(payload.decode("utf-8-sig"), newline=""), strict=True)
        header = next(reader)
        normalized_header = [str(column).strip().lower() for column in header]
        if not 3 <= len(header) <= 100:
            raise HTTPException(status_code=422, detail=f"{label} CSV must have between 3 and 100 columns")
        if len(set(normalized_header)) != len(normalized_header):
            raise HTTPException(status_code=422, detail=f"{label} CSV has duplicate column names")
        required = ("trans_id", "amount", "status")
        missing = sorted(set(required) - set(normalized_header))
        if missing:
            raise HTTPException(status_code=422, detail=f"{label} CSV is missing required columns: {', '.join(missing)}")
        indexes = [normalized_header.index(column) for column in required]
        records = []
        for row in reader:
            if not row:
                continue
            if len(row) != len(header):
                raise HTTPException(status_code=422, detail=f"{label} CSV rows must have the same number of columns as the header")
            record = [row[index] for index in indexes]
            if any("\x00" in value for value in row):
                raise HTTPException(status_code=422, detail=f"{label} CSV contains a NUL character")
            if len(records) >= 250_000:
                raise HTTPException(status_code=422, detail=f"{label} CSV must contain between 1 and 250,000 rows")
            if any(len(value.strip()) > limit for value, limit in zip(record, (100, 32, 100))):
                raise HTTPException(status_code=422, detail=f"{label} CSV contains an oversized transaction ID, amount, or status")
            records.append(record)
        # Build only required columns, as strings. Inference before normalizing
        # header names used to drop leading zeros from transaction identifiers.
        frame = pd.DataFrame(records, columns=required, dtype="string")
    except HTTPException:
        raise
    except (UnicodeDecodeError, pd.errors.ParserError, pd.errors.EmptyDataError, csv.Error, ValueError, StopIteration) as error:
        raise HTTPException(
            status_code=422,
            detail=f"{label} CSV cannot be parsed; verify its UTF-8 encoding and CSV quoting",
        ) from error

    if frame.empty or len(frame) > 250_000:
        raise HTTPException(status_code=422, detail=f"{label} CSV must contain between 1 and 250,000 rows")

    frame["trans_id"] = frame["trans_id"].astype("string").str.strip()
    if frame["trans_id"].isna().any() or frame["trans_id"].eq("").any():
        raise HTTPException(status_code=422, detail=f"{label} CSV contains a blank transaction ID")
    if frame["trans_id"].str.len().gt(100).any():
        raise HTTPException(status_code=422, detail=f"{label} transaction IDs must be 100 characters or fewer")
    if frame["trans_id"].duplicated().any():
        raise HTTPException(status_code=422, detail=f"{label} CSV contains duplicate transaction IDs")

    try:
        frame["amount"] = reconcile_engine.normalize_vnd_amounts(frame["amount"], label)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    frame["status"] = frame["status"].astype("string").str.strip().str.upper()
    if frame["status"].isna().any() or frame["status"].eq("").any():
        raise HTTPException(status_code=422, detail=f"{label} CSV contains a blank status")
    if frame["status"].str.len().gt(100).any():
        raise HTTPException(status_code=422, detail=f"{label} statuses must be 100 characters or fewer")
    return frame


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


@app.post("/api/upload-and-reconcile")
def upload_and_reconcile(
    core_file: UploadFile = File(...),
    partner_file: UploadFile = File(...),
    expected_run_id: str = Form(default=""),
    user=Depends(require_role("admin", "operator")),
):
    core_payload = core_file.file.read(config.MAX_UPLOAD_BYTES + 1)
    partner_payload = partner_file.file.read(config.MAX_UPLOAD_BYTES + 1)
    if len(core_payload) > config.MAX_UPLOAD_BYTES or len(partner_payload) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Each CSV file must be 10 MB or smaller")

    core_frame = parse_uploaded_csv(core_file.filename, core_payload, "Core")
    partner_frame = parse_uploaded_csv(partner_file.filename, partner_payload, "Partner")
    result_rows = reconcile_engine.build_reconciliation_rows(core_frame, partner_frame)

    run_id = uuid.uuid4().hex
    archive_dir = config.UPLOAD_DIR / run_id
    try:
        archive_dir.mkdir(parents=True, exist_ok=False)
        (archive_dir / "core.csv").write_bytes(core_payload)
        (archive_dir / "partner.csv").write_bytes(partner_payload)

        try:
            reconcile_engine.replace_reconciliation_results(
                result_rows, run_id=run_id, performed_by=user["username"],
                core_filename=Path(core_file.filename or "core.csv").name,
                partner_filename=Path(partner_file.filename or "partner.csv").name,
                expected_current_run_id=expected_run_id or None, enforce_expected=True,
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        mirror_warning = None
        try:
            atomic_write(BASE_DIR / "core_log.csv", core_payload)
            atomic_write(BASE_DIR / "partner_log.csv", partner_payload)
        except OSError:
            mirror_warning = "The reconciled database is saved; the convenience CSV copies could not be refreshed."

        return {
            "status": "success",
            "message": "Reconciliation completed",
            "count": len(result_rows),
            "upload_id": run_id,
            "warning": mirror_warning,
        }
    except Exception:
        shutil.rmtree(archive_dir, ignore_errors=True)
        raise


@app.post("/api/upload-preview")
def preview_upload(
    core_file: UploadFile = File(...), partner_file: UploadFile = File(...),
    _user=Depends(require_role("admin", "operator")),
):
    core_payload = core_file.file.read(config.MAX_UPLOAD_BYTES + 1)
    partner_payload = partner_file.file.read(config.MAX_UPLOAD_BYTES + 1)
    if len(core_payload) > config.MAX_UPLOAD_BYTES or len(partner_payload) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Each CSV file must be 10 MB or smaller")
    core = parse_uploaded_csv(core_file.filename, core_payload, "Core")
    partner = parse_uploaded_csv(partner_file.filename, partner_payload, "Partner")
    try:
        rows = reconcile_engine.build_reconciliation_rows(core, partner)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    counts: dict[str, int] = {}
    for row in rows:
        counts[row[5]] = counts.get(row[5], 0) + 1
    conn = get_db_connection()
    try:
        current = conn.execute("SELECT run_id FROM reconciliation_results LIMIT 1").fetchone()
    finally:
        conn.close()
    return {"transaction_count": len(rows), "matched_count": counts.get("MATCHED", 0),
            "discrepancy_count": len(rows) - counts.get("MATCHED", 0), "by_status": counts,
            "current_run_id": current[0] if current else None}
