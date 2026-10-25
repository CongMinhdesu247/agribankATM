"""Durable, verifiable CSV handoff for pausing and resuming reconciliation runs."""

import csv
import hashlib
import hmac
import io
import re
import uuid
from contextlib import closing
from datetime import datetime, timezone

from . import config
from .database import connect_database, get_table_columns

MAX_CSV_BYTES = 64 * 1024 * 1024
CSV_COLUMNS = (
    "record_type", "format_version", "suspension_id", "run_id", "exported_at", "exported_by", "transaction_count",
    "trans_id", "core_amount", "partner_amount", "core_status", "partner_status", "recon_status",
    "processing_status", "pending_log_id", "action_type", "napas_ref_code", "reason", "performed_by", "proposed_at",
)
ITEM_COLUMNS = CSV_COLUMNS[7:]


def initialize_schema(conn):
    columns = get_table_columns(conn, "reconciliation_runs")
    for name in ("suspended_at", "suspended_by", "active_suspension_id"):
        if name not in columns:
            conn.execute(f"ALTER TABLE reconciliation_runs ADD COLUMN {name} TEXT")
    conn.execute("""CREATE TABLE IF NOT EXISTS reconciliation_suspensions (
        suspension_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES reconciliation_runs(run_id),
        exported_at TEXT NOT NULL,
        exported_by TEXT NOT NULL,
        transaction_count INTEGER NOT NULL,
        pending_count INTEGER NOT NULL,
        csv_hash TEXT NOT NULL,
        file_size INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('PREPARED', 'READY', 'CONSUMED')),
        resumed_at TEXT,
        resumed_by TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS reconciliation_suspension_items (
        suspension_id TEXT NOT NULL REFERENCES reconciliation_suspensions(suspension_id) ON DELETE CASCADE,
        trans_id TEXT NOT NULL,
        core_amount DOUBLE PRECISION,
        partner_amount DOUBLE PRECISION,
        core_status TEXT,
        partner_status TEXT,
        recon_status TEXT NOT NULL,
        processing_status TEXT NOT NULL,
        pending_log_id BIGINT,
        action_type TEXT,
        napas_ref_code TEXT,
        reason TEXT,
        performed_by TEXT,
        proposed_at TEXT,
        PRIMARY KEY (suspension_id, trans_id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_suspensions_run ON reconciliation_suspensions (run_id, status)")
    conn.execute("INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (3, CURRENT_TIMESTAMP)")


def _safe_cell(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).replace("\r", " ").replace("\n", " ")
    return "'" + text if text.lstrip(" \t").startswith(("=", "+", "-", "@")) else text


def csv_chunks(conn, suspension):
    """Recreate exactly the same CSV from immutable export snapshots."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")

    def line(values):
        writer.writerow(values)
        encoded = buffer.getvalue().encode("utf-8")
        buffer.seek(0)
        buffer.truncate(0)
        return encoded

    yield b"\xef\xbb\xbf" + line(CSV_COLUMNS)
    yield line(["MANIFEST", "1", suspension["suspension_id"], suspension["run_id"], suspension["exported_at"],
                _safe_cell(suspension["exported_by"]), suspension["transaction_count"], *([""] * len(ITEM_COLUMNS))])
    rows = conn.execute("""SELECT trans_id, core_amount, partner_amount, core_status, partner_status, recon_status,
        processing_status, pending_log_id, action_type, napas_ref_code, reason, performed_by, proposed_at
        FROM reconciliation_suspension_items WHERE suspension_id = ? ORDER BY trans_id""", (suspension["suspension_id"],))
    for row in rows:
        yield line(["TRANSACTION", *([""] * 6), *(_safe_cell(row[column]) for column in ITEM_COLUMNS)])


def get_export(conn, suspension_id):
    row = conn.execute("SELECT * FROM reconciliation_suspensions WHERE suspension_id = ?", (suspension_id,)).fetchone()
    if row is None:
        raise ValueError("Không tìm thấy đợt đã lưu trong cơ sở dữ liệu này.")
    return row


def _event(conn, run_id, username, action, reason):
    conn.execute("""INSERT INTO audit_logs
        (trans_id, action_type, napas_ref_code, reason, performed_by, run_id, review_status)
        VALUES ('*', ?, '-', ?, ?, ?, 'APPROVED')""", (action, reason, username, run_id))


def prepare_export(run_id, username):
    from .reconcile_engine import current_workflow
    with closing(connect_database(config.DB_PATH)) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            state = current_workflow(conn)
            if state["run_id"] != run_id or state["closed"]:
                raise ValueError("Đợt đã thay đổi hoặc đã đóng. Vui lòng tải lại dữ liệu.")
            if state["suspended"]:
                result = dict(get_export(conn, state["suspension_id"]))
                conn.commit()
                return result
            if not state["can_suspend"]:
                raise ValueError("Không còn công việc cần lưu. Hãy đóng đợt đã hoàn tất để mở đợt mới.")
            suspension_id = uuid.uuid4().hex
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            conn.execute("""INSERT INTO reconciliation_suspensions
                (suspension_id, run_id, exported_at, exported_by, transaction_count, pending_count,
                 csv_hash, file_size, status) VALUES (?, ?, ?, ?, ?, ?, '', 0, 'PREPARED')""",
                (suspension_id, run_id, now, username, state["unresolved_count"], state["pending_count"]))
            conn.execute("""INSERT INTO reconciliation_suspension_items
                (suspension_id, trans_id, core_amount, partner_amount, core_status, partner_status, recon_status,
                 processing_status, pending_log_id, action_type, napas_ref_code, reason, performed_by, proposed_at)
                SELECT ?, r.trans_id, r.core_amount, r.partner_amount, r.core_status, r.partner_status, r.recon_status,
                       CASE WHEN a.log_id IS NULL THEN 'UNPROCESSED' ELSE 'PENDING' END,
                       a.log_id, a.action_type, a.napas_ref_code, a.reason, a.performed_by, a.created_at
                FROM reconciliation_results r LEFT JOIN audit_logs a ON a.trans_id = r.trans_id
                     AND a.run_id = r.run_id AND a.review_status = 'PENDING'
                WHERE r.run_id = ? AND r.resolved = 0 AND r.recon_status != 'MATCHED'""", (suspension_id, run_id))
            conn.execute("""UPDATE reconciliation_runs SET suspended_at = ?, suspended_by = ?, active_suspension_id = ?
                WHERE run_id = ?""", (now, username, suspension_id, run_id))
            suspension = get_export(conn, suspension_id)
            digest, size = hashlib.sha256(), 0
            for chunk in csv_chunks(conn, suspension):
                size += len(chunk)
                if size > MAX_CSV_BYTES:
                    raise ValueError("File công việc vượt giới hạn 64 MB; chưa tạm dừng đợt. Vui lòng xử lý bớt trước.")
                digest.update(chunk)
            conn.execute("UPDATE reconciliation_suspensions SET csv_hash = ?, file_size = ? WHERE suspension_id = ?",
                         (digest.hexdigest(), size, suspension_id))
            _event(conn, run_id, username, "SUSPEND_RUN", "Lưu CSV công việc và tạm dừng đợt để xử lý khẩn")
            result = dict(get_export(conn, suspension_id))
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def acknowledge_export(suspension_id, run_id, csv_hash, username):
    from .reconcile_engine import current_workflow
    with closing(connect_database(config.DB_PATH)) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            suspension = get_export(conn, suspension_id)
            if suspension["run_id"] != run_id or suspension["status"] == "CONSUMED":
                raise ValueError("File này không còn là bản tạm dừng đang có hiệu lực.")
            if not hmac.compare_digest(suspension["csv_hash"], csv_hash):
                raise ValueError("File CSV chưa được tải đầy đủ hoặc đã bị thay đổi.")
            if suspension["status"] == "PREPARED":
                state = current_workflow(conn)
                if state["run_id"] != run_id or state["suspension_id"] != suspension_id:
                    raise ValueError("Đợt đã thay đổi. Vui lòng tải lại dữ liệu.")
                conn.execute("UPDATE reconciliation_suspensions SET status = 'READY' WHERE suspension_id = ?", (suspension_id,))
                _event(conn, run_id, username, "CONFIRM_SUSPENSION_EXPORT", "Đã nhận đủ CSV; cho phép mở đợt khẩn")
            result = current_workflow(conn)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def inspect_manifest(payload):
    if not payload or len(payload) > MAX_CSV_BYTES:
        raise ValueError("File CSV phải có dữ liệu và không vượt quá 64 MB.")
    try:
        # Only the small header/manifest needs parsing. The remaining bytes are
        # verified against the stored hash, never trusted as financial input.
        header_end = payload.find(b"\n", 0, 4096)
        manifest_end = payload.find(b"\n", header_end + 1, 8192) if header_end >= 0 else -1
        if manifest_end < 0:
            raise ValueError("Thiếu thông tin bản lưu trong CSV công việc.")
        reader = csv.reader(io.StringIO(payload[:manifest_end + 1].decode("utf-8-sig"), newline=""), strict=True)
        if next(reader) != list(CSV_COLUMNS):
            raise ValueError("Không đúng mẫu CSV công việc do ứng dụng xuất.")
        manifest = next(reader)
        if (len(manifest) != len(CSV_COLUMNS) or manifest[:2] != ["MANIFEST", "1"]
                or not re.fullmatch(r"[0-9a-f]{32}", manifest[2])
                or not re.fullmatch(r"[0-9a-f]{32}", manifest[3])):
            raise ValueError("Thông tin đợt trong file CSV không hợp lệ.")
        return manifest[2], manifest[3]
    except (UnicodeError, csv.Error, StopIteration) as error:
        raise ValueError("Không đọc được CSV công việc. Hãy dùng nguyên file đã tải xuống từ ứng dụng.") from error


def resume_export(payload, expected_run_id, username):
    from .reconcile_engine import current_workflow
    suspension_id, run_id = inspect_manifest(payload)
    digest = hashlib.sha256(payload).hexdigest()
    with closing(connect_database(config.DB_PATH)) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            suspension = get_export(conn, suspension_id)
            if suspension["run_id"] != run_id or not hmac.compare_digest(suspension["csv_hash"], digest):
                raise ValueError("File CSV đã bị sửa hoặc không thuộc bản lưu này. Hãy tải lại bản gốc từ danh sách tạm dừng.")
            if suspension["status"] == "CONSUMED":
                raise ValueError("Bản CSV này đã được tiếp tục xử lý; không thể nhập lại để tạo công việc trùng.")
            state = current_workflow(conn)
            if state["run_id"] != expected_run_id:
                raise ValueError("Đợt hiện tại đã thay đổi. Vui lòng tải lại trước khi nhập CSV.")
            same_suspended_run = state["run_id"] == run_id and state["suspended"]
            if not same_suspended_run and not state["can_upload"]:
                raise ValueError("Cần hoàn tất và đóng đợt khẩn hiện tại, hoặc lưu tạm đợt đó trước khi tiếp tục đợt cũ.")
            target = conn.execute("SELECT * FROM reconciliation_runs WHERE run_id = ?", (run_id,)).fetchone()
            if (not target or not target["suspended_at"] or target["closed_at"]
                    or target["active_suspension_id"] != suspension_id):
                raise ValueError("Đợt lưu trong CSV không còn ở trạng thái tạm dừng.")
            conn.execute("DELETE FROM reconciliation_results")
            conn.execute("""INSERT INTO reconciliation_results
                (trans_id, core_amount, partner_amount, core_status, partner_status, recon_status, resolved, created_at, run_id)
                SELECT trans_id, core_amount, partner_amount, core_status, partner_status, recon_status, resolved, ?, run_id
                FROM reconciliation_run_results WHERE run_id = ?""", (target["created_at"], run_id))
            conn.execute("""UPDATE reconciliation_runs SET suspended_at = NULL, suspended_by = NULL,
                active_suspension_id = NULL WHERE run_id = ?""", (run_id,))
            conn.execute("""UPDATE reconciliation_suspensions SET status = 'CONSUMED', resumed_at = ?, resumed_by = ?
                WHERE suspension_id = ?""", (datetime.now(timezone.utc).isoformat(timespec="seconds"), username, suspension_id))
            _event(conn, run_id, username, "RESUME_RUN", "Tiếp tục đợt từ CSV đã kiểm tra toàn vẹn; giữ nguyên yêu cầu phê duyệt")
            result = current_workflow(conn)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
