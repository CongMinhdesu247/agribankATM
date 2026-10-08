from pathlib import Path
from datetime import datetime
import csv
import io
import hmac
import math
import os
import shutil
import tempfile
import time
import uuid
from io import BytesIO
from typing import Literal
import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
import sqlite3
import config
import reconcile_engine
from security import create_access_token, require_auth, verify_password

if len(config.JWT_SECRET) < 32 or not config.APP_USERNAME or not config.APP_PASSWORD_HASH:
    raise RuntimeError("Authentication is not configured. Run setup_admin.py to create the .env file.")

app = FastAPI(title="Agribank Reconciliation Review", docs_url=None, redoc_url=None, openapi_url=None)
BASE_DIR = config.BASE_DIR
DB_PATH = config.DB_PATH
reconcile_engine.initialize_database()

@app.middleware("http")
async def set_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response

class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=256)

LOGIN_ATTEMPTS: dict[str, list[float]] = {}

@app.post("/api/login")
def login(req: LoginRequest, request: Request):
    now = time.monotonic()
    client_ip = request.client.host if request.client else "unknown"
    if len(LOGIN_ATTEMPTS) > 4096:
        cutoff = now - 300
        for ip, stamps in list(LOGIN_ATTEMPTS.items()):
            active = [stamp for stamp in stamps if stamp >= cutoff]
            if active:
                LOGIN_ATTEMPTS[ip] = active
            else:
                LOGIN_ATTEMPTS.pop(ip, None)
    attempts = [stamp for stamp in LOGIN_ATTEMPTS.get(client_ip, []) if now - stamp < 300]
    if len(attempts) >= 5:
        raise HTTPException(status_code=429, detail="Too many failed login attempts; try again in five minutes")

    username_ok = hmac.compare_digest(req.username.strip().encode("utf-8"), config.APP_USERNAME.encode("utf-8"))
    password_ok = verify_password(req.password, config.APP_PASSWORD_HASH)
    if not username_ok or not password_ok:
        attempts.append(now)
        LOGIN_ATTEMPTS[client_ip] = attempts
        raise HTTPException(status_code=401, detail="Invalid username or password")
    LOGIN_ATTEMPTS.pop(client_ip, None)
    token, expires_in = create_access_token(config.APP_USERNAME)
    return {"access_token": token, "token_type": "bearer", "expires_in": expires_in, "username": config.APP_USERNAME}

# Serve static assets from the project directory.
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# 3. Trả về giao diện web trực tiếp từ Server
@app.get("/")
def read_root():
    return FileResponse(
        BASE_DIR / "index.html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )

def get_db_connection():
    # Sửa 'reconciliation.db' thành 'agribank_recon.db'
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_audit_report_columns(conn):
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(audit_logs)")}
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

def ensure_initial_baseline(conn):
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reconciliation_baseline (
            trans_id TEXT PRIMARY KEY,
            core_amount REAL,
            partner_amount REAL,
            core_status TEXT,
            partner_status TEXT,
            recon_status TEXT NOT NULL,
            resolved INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    if cursor.execute("SELECT COUNT(*) FROM reconciliation_baseline").fetchone()[0]:
        return

    current_count = cursor.execute("SELECT COUNT(*) FROM reconciliation_results").fetchone()[0]
    if current_count:
        cursor.execute('''
            INSERT INTO reconciliation_baseline
                (trans_id, core_amount, partner_amount, core_status, partner_status, recon_status, resolved, created_at)
            SELECT trans_id, core_amount, partner_amount, core_status, partner_status, recon_status, resolved, created_at
            FROM reconciliation_results
        ''')

# --- CÁC API KHÁC GIỮ NGUYÊN ---
@app.get("/api/stats")
def get_stats(_username: str = Depends(require_auth)):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT recon_status, COUNT(*) as total 
        FROM reconciliation_results 
        WHERE resolved = 0 AND recon_status != 'MATCHED'
        GROUP BY recon_status
    ''')
    rows = cursor.fetchall()
    stats = {row['recon_status']: row['total'] for row in rows}
    
    cursor.execute("SELECT COUNT(*) AS total FROM reconciliation_results WHERE recon_status = 'MATCHED'")
    stats['MATCHED'] = cursor.fetchone()['total']
    conn.close()
    return {"status": "success", "data": stats}

@app.get("/api/anomalies")
def get_anomalies(_username: str = Depends(require_auth)):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT * FROM reconciliation_results 
        WHERE recon_status != 'MATCHED' AND resolved = 0
        ORDER BY trans_id ASC
    ''')
    rows = cursor.fetchall()
    conn.close()
    return {"status": "success", "count": len(rows), "data": [dict(row) for row in rows]}

@app.get("/api/transactions")
def get_transactions(_username: str = Depends(require_auth)):
    conn = get_db_connection()
    rows = conn.execute('''
        SELECT * FROM reconciliation_results
        ORDER BY trans_id ASC
    ''').fetchall()
    conn.close()
    return {"status": "success", "count": len(rows), "data": [dict(row) for row in rows]}

@app.post("/api/restore-baseline")
def restore_baseline(_username: str = Depends(require_auth)):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if cursor.execute("SELECT COUNT(*) FROM reconciliation_baseline").fetchone()[0] == 0:
            raise HTTPException(status_code=409, detail="No baseline dataset is available to restore")
        cursor.execute("DELETE FROM reconciliation_results")
        cursor.execute('''
            INSERT INTO reconciliation_results
                (trans_id, core_amount, partner_amount, core_status, partner_status, recon_status, resolved, created_at)
            SELECT trans_id, core_amount, partner_amount, core_status, partner_status, recon_status, resolved, created_at
            FROM reconciliation_baseline
        ''')
        conn.commit()
        count = cursor.execute("SELECT COUNT(*) FROM reconciliation_results").fetchone()[0]
        return {"status": "success", "count": count, "message": "Đã khôi phục danh sách ban đầu"}
    except Exception as error:
        conn.rollback()
        if isinstance(error, HTTPException):
            raise
        raise HTTPException(status_code=500, detail=str(error))
    finally:
        conn.close()

class ResolveRequest(BaseModel):
    trans_id: str = Field(min_length=1, max_length=100)
    action_type: Literal["FORCE_SUCCESS", "REFUND"]
    napas_ref_code: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)

@app.post("/api/resolve")
def resolve_anomaly(req: ResolveRequest, username: str = Depends(require_auth)):
    conn = get_db_connection()
    try:
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

        cursor.execute("UPDATE reconciliation_results SET resolved = 1 WHERE trans_id = ?", (req.trans_id,))
        cursor.execute('''
            INSERT INTO audit_logs
                (trans_id, action_type, napas_ref_code, reason, performed_by,
                 core_amount, partner_amount, core_status, partner_status, recon_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            req.trans_id, req.action_type, req.napas_ref_code.strip(), req.reason.strip(), username,
            row["core_amount"], row["partner_amount"], row["core_status"], row["partner_status"], row["recon_status"],
        ))
        conn.commit()
        return {"status": "success", "message": "Transaction review saved"}
    except HTTPException:
        conn.rollback()
        raise
    except Exception as error:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(error))
    finally:
        conn.close()

@app.get("/api/reports/resolved.csv")
def export_resolved_report(_username: str = Depends(require_auth)):
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
                   a.action_type, a.napas_ref_code, a.reason, a.performed_by, a.created_at
            FROM audit_logs AS a
            LEFT JOIN reconciliation_results AS r
                ON r.trans_id = a.trans_id AND r.created_at <= a.created_at
            ORDER BY a.created_at ASC, a.log_id ASC
        ''')
    except Exception:
        conn.close()
        raise

    headers = [
            "Mã log", "Mã giao dịch", "Tiền Core", "Tiền Napas", "Trạng thái Core",
            "Trạng thái Napas", "Kết quả đối soát", "Hình thức xử lý", "Mã tham chiếu Napas",
            "Lý do/Ghi chú", "Nhân viên thực hiện", "Thời gian xử lý",
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
                    csv_safe_text(row["performed_by"]), csv_safe_text(row["created_at"]),
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
def get_audit_logs(_username: str = Depends(require_auth)):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT 100")
    rows = cursor.fetchall()
    conn.close()
    return {"status": "success", "count": len(rows), "data": [dict(row) for row in rows]}

def parse_uploaded_csv(filename: str | None, payload: bytes, label: str) -> pd.DataFrame:
    if not filename or Path(filename).suffix.lower() != ".csv":
        raise HTTPException(status_code=415, detail=f"{label} file must have a .csv extension")
    if not payload:
        raise HTTPException(status_code=422, detail=f"{label} CSV is empty")
    if len(payload) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"{label} CSV exceeds the 10 MB limit")

    try:
        header = next(csv.reader(io.StringIO(payload.decode("utf-8-sig"), newline="")))
        normalized_header = [str(column).strip().lower() for column in header]
        if len(set(normalized_header)) != len(normalized_header):
            raise HTTPException(status_code=422, detail=f"{label} CSV has duplicate column names")
        frame = pd.read_csv(BytesIO(payload), encoding="utf-8-sig", dtype={"trans_id": "string"})
    except HTTPException:
        raise
    except (UnicodeDecodeError, pd.errors.ParserError, pd.errors.EmptyDataError, csv.Error, ValueError, StopIteration) as error:
        raise HTTPException(status_code=422, detail=f"{label} CSV cannot be parsed: {error}") from error

    frame.columns = [str(column).strip().lower() for column in frame.columns]
    if len(set(frame.columns)) != len(frame.columns):
        raise HTTPException(status_code=422, detail=f"{label} CSV has duplicate column names")
    required = {"trans_id", "amount", "status"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise HTTPException(status_code=422, detail=f"{label} CSV is missing required columns: {', '.join(missing)}")
    if frame.empty or len(frame) > 250_000:
        raise HTTPException(status_code=422, detail=f"{label} CSV must contain between 1 and 250,000 rows")

    frame = frame[["trans_id", "amount", "status"]].copy()
    frame["trans_id"] = frame["trans_id"].astype("string").str.strip()
    if frame["trans_id"].isna().any() or frame["trans_id"].eq("").any():
        raise HTTPException(status_code=422, detail=f"{label} CSV contains a blank transaction ID")
    if frame["trans_id"].duplicated().any():
        raise HTTPException(status_code=422, detail=f"{label} CSV contains duplicate transaction IDs")

    frame["amount"] = pd.to_numeric(frame["amount"], errors="coerce")
    if frame["amount"].isna().any() or frame["amount"].lt(0).any() or not frame["amount"].map(math.isfinite).all():
        raise HTTPException(status_code=422, detail=f"{label} CSV amounts must be valid non-negative numbers")
    frame["status"] = frame["status"].astype("string").str.strip().str.upper()
    if frame["status"].isna().any() or frame["status"].eq("").any():
        raise HTTPException(status_code=422, detail=f"{label} CSV contains a blank status")
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
async def upload_and_reconcile(
    core_file: UploadFile = File(...),
    partner_file: UploadFile = File(...),
    _username: str = Depends(require_auth),
):
    core_payload = await core_file.read(config.MAX_UPLOAD_BYTES + 1)
    partner_payload = await partner_file.read(config.MAX_UPLOAD_BYTES + 1)
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

        baseline_conn = get_db_connection()
        try:
            ensure_initial_baseline(baseline_conn)
            baseline_conn.commit()
        finally:
            baseline_conn.close()

        reconcile_engine.replace_reconciliation_results(result_rows)
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
