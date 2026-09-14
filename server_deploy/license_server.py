"""MixcutAI cloud licensing service.

Trust boundary:
* Ed25519 private key exists only on this server.
* desktop clients contain only the public verification key.
* online decisions are stored in SQLite and every activation/check/unbind is logged.
* signed offline entitlements remain usable and are imported on first online check.
"""

from __future__ import annotations

import base64
import contextlib
import functools
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash


PRODUCT_ID = "mixcut-ai-windows"
TOKEN_PREFIX = "MXLIC1"
VALID_TYPES = {"day1": 1, "day7": 7, "month": 30, "year": 365, "permanent": 0}
SHORT_TERM_TYPES = {"day1", "day7"}
TYPE_LABELS = {"day1": "1天卡", "day7": "7天卡", "month": "月卡", "year": "年卡", "permanent": "永久卡"}
DATA_DIR = Path(os.environ.get("MIXCUT_DATA_DIR", "/var/lib/mixcut"))
DB_PATH = DATA_DIR / "licenses.db"
PRIVATE_KEY_PATH = Path(os.environ.get("MIXCUT_PRIVATE_KEY", "/etc/mixcut/license_private_key.pem"))
PEPPER_PATH = Path(os.environ.get("MIXCUT_PEPPER_FILE", "/etc/mixcut/key_pepper"))
SESSION_SECRET_PATH = Path(os.environ.get("MIXCUT_SESSION_SECRET", "/etc/mixcut/admin_session_secret"))

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config.update(
    MAX_CONTENT_LENGTH=32 * 1024,
    SECRET_KEY=SESSION_SECRET_PATH.read_text(encoding="ascii").strip() if SESSION_SECRET_PATH.exists() else secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Strict",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def calculate_expiry(kind: str, activated_at: str, duration_days: int) -> str:
    """Keep legacy month/year date semantics; short cards use exact UTC hours."""
    if kind == "permanent":
        return ""
    if kind in SHORT_TERM_TYPES:
        return iso_utc(parse_utc(activated_at) + timedelta(days=int(duration_days)))
    return (date.today() + timedelta(days=int(duration_days))).isoformat()


def expiry_reached(kind: str, expire_date: str, current_time: datetime | None = None) -> bool:
    if kind == "permanent" or not expire_date:
        return False
    if kind in SHORT_TERM_TYPES:
        return (current_time or utc_now()).astimezone(timezone.utc) >= parse_utc(expire_date)
    comparison_date = current_time.date() if current_time is not None else date.today()
    return date.fromisoformat(expire_date) < comparison_date


def canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def signing_key():
    return serialization.load_pem_private_key(PRIVATE_KEY_PATH.read_bytes(), password=None)


def pepper() -> bytes:
    return PEPPER_PATH.read_bytes().strip()


def normalize_key(value: str) -> str:
    return "-".join(part for part in str(value or "").upper().replace(" ", "").split("-") if part)


def key_digest(value: str) -> str:
    return hmac.new(pepper(), normalize_key(value).encode("ascii", "ignore"), hashlib.sha256).hexdigest()


@contextlib.contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_column(conn: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS customers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                company TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS license_keys (
                id TEXT PRIMARY KEY,
                key_hash TEXT NOT NULL UNIQUE,
                key_hint TEXT NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('day1','day7','month','year','permanent')),
                duration_days INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'issued',
                machine_id TEXT NOT NULL DEFAULT '',
                activated_at TEXT NOT NULL DEFAULT '',
                expire_date TEXT NOT NULL DEFAULT '',
                unbind_count INTEGER NOT NULL DEFAULT 0,
                max_unbinds INTEGER NOT NULL DEFAULT 2,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                license_id TEXT NOT NULL REFERENCES license_keys(id),
                machine_id TEXT NOT NULL,
                device_name TEXT NOT NULL DEFAULT '',
                os_info TEXT NOT NULL DEFAULT '',
                app_version TEXT NOT NULL DEFAULT '',
                first_ip TEXT NOT NULL DEFAULT '',
                last_ip TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                bound_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                unbound_at TEXT NOT NULL DEFAULT '',
                UNIQUE(license_id,machine_id)
            );
            CREATE TABLE IF NOT EXISTS activation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                license_id TEXT NOT NULL DEFAULT '',
                customer_id TEXT NOT NULL DEFAULT '',
                device_id TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL,
                result TEXT NOT NULL,
                ip_address TEXT NOT NULL DEFAULT '',
                user_agent TEXT NOT NULL DEFAULT '',
                machine_id TEXT NOT NULL DEFAULT '',
                app_version TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT NOT NULL,
                license_id TEXT NOT NULL DEFAULT '',
                machine_id TEXT NOT NULL DEFAULT '',
                ip_address TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                last_login_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_license_machine ON license_keys(machine_id);
            CREATE INDEX IF NOT EXISTS idx_devices_machine ON devices(machine_id);
            CREATE INDEX IF NOT EXISTS idx_activation_created ON activation_logs(created_at);
            """
        )
    migrate_license_type_constraint()
    with connect() as conn:
        ensure_column(conn, "license_keys", "customer_id", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "license_keys", "max_devices", "INTEGER NOT NULL DEFAULT 1")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("授权数据库完整性检查失败")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("授权数据库外键检查失败")


def migrate_license_type_constraint() -> None:
    """Expand the legacy CHECK constraint without changing any existing row values."""
    if not DB_PATH.exists():
        return
    with contextlib.closing(sqlite3.connect(DB_PATH)) as probe:
        row = probe.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='license_keys'").fetchone()
        schema_sql = str(row[0] or "") if row else ""
    if "'day1'" in schema_sql and "'day7'" in schema_sql:
        return

    backup = DB_PATH.with_name(f"{DB_PATH.stem}.pre-short-license-{utc_now().strftime('%Y%m%d%H%M%S')}{DB_PATH.suffix}")
    with contextlib.closing(sqlite3.connect(DB_PATH)) as source, contextlib.closing(sqlite3.connect(backup)) as target:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("授权数据库迁移前备份校验失败")

    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """CREATE TABLE license_keys_new (
                id TEXT PRIMARY KEY,key_hash TEXT NOT NULL UNIQUE,key_hint TEXT NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('day1','day7','month','year','permanent')),
                duration_days INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'issued',
                machine_id TEXT NOT NULL DEFAULT '',activated_at TEXT NOT NULL DEFAULT '',
                expire_date TEXT NOT NULL DEFAULT '',unbind_count INTEGER NOT NULL DEFAULT 0,
                max_unbinds INTEGER NOT NULL DEFAULT 2,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                customer_id TEXT NOT NULL DEFAULT '',max_devices INTEGER NOT NULL DEFAULT 1
            )"""
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(license_keys)")}
        ordered = ["id", "key_hash", "key_hint", "type", "duration_days", "status", "machine_id",
                   "activated_at", "expire_date", "unbind_count", "max_unbinds", "created_at", "updated_at"]
        select_parts = ordered[:]
        for name, default in (("customer_id", "''"), ("max_devices", "1")):
            ordered.append(name)
            select_parts.append(name if name in columns else default)
        conn.execute(
            f"INSERT INTO license_keys_new({','.join(ordered)}) SELECT {','.join(select_parts)} FROM license_keys"
        )
        conn.execute("DROP TABLE license_keys")
        conn.execute("ALTER TABLE license_keys_new RENAME TO license_keys")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_license_machine ON license_keys(machine_id)")
        conn.commit()
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("授权数据库迁移后完整性检查失败")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def request_ip() -> str:
    # Caddy is the only trusted upstream and overwrites X-Forwarded-For.
    forwarded = str(request.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
    return (forwarded or request.remote_addr or "")[:64]


def log_event(conn: sqlite3.Connection, action: str, result: str, *, row: sqlite3.Row | None = None,
              device_id: str = "", machine_id: str = "", app_version: str = "", message: str = "") -> None:
    license_id = str(row["id"]) if row else ""
    customer_id = str(row["customer_id"] or "") if row and "customer_id" in row.keys() else ""
    conn.execute(
        """INSERT INTO activation_logs
           (license_id,customer_id,device_id,action,result,ip_address,user_agent,machine_id,app_version,message,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (license_id, customer_id, device_id, action, result, request_ip(),
         str(request.headers.get("User-Agent") or "")[:300], mask_machine_id(machine_id), app_version[:32], message[:500], iso_utc()),
    )


def mask_machine_id(value: str) -> str:
    """Keep full machine IDs only in the devices binding table, never in logs."""
    machine = str(value or "").strip()
    if not machine:
        return ""
    if len(machine) <= 12:
        return "***"
    return f"{machine[:7]}...{machine[-5:]}"


def issue_token(row: sqlite3.Row, machine_id: str) -> str:
    payload = {
        "schema": 1,
        "license_id": row["id"],
        "product_id": PRODUCT_ID,
        "type": row["type"],
        "machine_id": machine_id,
        "issued_at": iso_utc(),
        "activated_at": row["activated_at"],
        "expire_date": row["expire_date"],
        "nonce": secrets.token_urlsafe(18),
        "max_devices": int(row["max_devices"] or 1),
        "transfer_policy": "server_approval",
    }
    encoded = canonical(payload)
    return f"{TOKEN_PREFIX}.{b64url(encoded)}.{b64url(signing_key().sign(encoded))}"


def verify_token(token: str) -> dict[str, Any]:
    try:
        prefix, payload_part, signature_part = str(token or "").split(".", 2)
        if prefix != TOKEN_PREFIX:
            raise ValueError("bad prefix")
        payload_bytes = b64decode(payload_part)
        signing_key().public_key().verify(b64decode(signature_part), payload_bytes)
        payload = json.loads(payload_bytes.decode("utf-8"))
        if canonical(payload) != payload_bytes:
            raise ValueError("non-canonical payload")
        return payload
    except (ValueError, TypeError, json.JSONDecodeError, InvalidSignature) as exc:
        raise ValueError("授权凭证无效") from exc


def require_payload() -> dict[str, Any]:
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ValueError("请求格式无效")
    if value.get("product_id") != PRODUCT_ID:
        raise ValueError("产品标识无效")
    return value


def valid_machine(value: Any) -> str:
    machine = str(value or "").strip().upper()
    if not machine.startswith("MX-") or len(machine) > 64:
        raise ValueError("机器码格式无效")
    return machine


def upsert_device(conn: sqlite3.Connection, row: sqlite3.Row, machine_id: str, data: dict[str, Any]) -> sqlite3.Row:
    device = conn.execute("SELECT * FROM devices WHERE license_id=? AND machine_id=?", (row["id"], machine_id)).fetchone()
    if not device:
        active_count = conn.execute("SELECT COUNT(*) FROM devices WHERE license_id=? AND status='active'", (row["id"],)).fetchone()[0]
        if active_count >= int(row["max_devices"] or 1):
            raise ValueError("授权设备数量已达到上限")
        device_id = str(uuid.uuid4())
        stamp = iso_utc()
        conn.execute(
            """INSERT INTO devices
               (id,license_id,machine_id,device_name,os_info,app_version,first_ip,last_ip,status,bound_at,last_seen_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (device_id, row["id"], machine_id, str(data.get("device_name") or "")[:120],
             str(data.get("os_info") or "")[:200], str(data.get("app_version") or "")[:32],
             request_ip(), request_ip(), "active", stamp, stamp),
        )
    else:
        if device["status"] != "active":
            active_count = conn.execute("SELECT COUNT(*) FROM devices WHERE license_id=? AND status='active'", (row["id"],)).fetchone()[0]
            if active_count >= int(row["max_devices"] or 1):
                raise ValueError("授权设备数量已达到上限")
        conn.execute(
            """UPDATE devices SET status='active',device_name=?,os_info=?,app_version=?,last_ip=?,last_seen_at=?,unbound_at=''
               WHERE id=?""",
            (str(data.get("device_name") or device["device_name"])[:120],
             str(data.get("os_info") or device["os_info"])[:200], str(data.get("app_version") or "")[:32],
             request_ip(), iso_utc(), device["id"]),
        )
    return conn.execute("SELECT * FROM devices WHERE license_id=? AND machine_id=?", (row["id"], machine_id)).fetchone()


def active_row_for_token(conn: sqlite3.Connection, data: dict[str, Any], machine_id: str) -> tuple[sqlite3.Row, sqlite3.Row]:
    payload = verify_token(str(data.get("license_token") or ""))
    if payload.get("product_id") != PRODUCT_ID or payload.get("machine_id") != machine_id:
        raise ValueError("授权与当前机器不匹配")
    license_id = str(payload.get("license_id") or "")
    row = conn.execute("SELECT * FROM license_keys WHERE id=?", (license_id,)).fetchone()
    if not row:
        kind = str(payload.get("type") or "")
        expire_date = str(payload.get("expire_date") or "")
        if kind not in VALID_TYPES:
            raise ValueError("授权类型无效")
        if kind != "permanent":
            try:
                parse_utc(expire_date) if kind in SHORT_TERM_TYPES else date.fromisoformat(expire_date)
            except ValueError as exc:
                raise ValueError("授权有效期无效") from exc
        stamp = iso_utc()
        imported_hash = hmac.new(pepper(), ("imported:" + license_id).encode(), hashlib.sha256).hexdigest()
        conn.execute(
            """INSERT INTO license_keys
               (id,key_hash,key_hint,type,duration_days,status,machine_id,activated_at,expire_date,created_at,updated_at)
               VALUES(?,?,?,?,?,'active',?,?,?,?,?)""",
            (license_id, imported_hash, "IMPRT", kind, VALID_TYPES[kind], machine_id,
             str(payload.get("activated_at") or payload.get("issued_at") or stamp), expire_date, stamp, stamp),
        )
        row = conn.execute("SELECT * FROM license_keys WHERE id=?", (license_id,)).fetchone()
    if row["status"] in {"frozen", "revoked", "disabled"}:
        raise ValueError("授权已被冻结或停用")
    if row["status"] == "expired" or expiry_reached(row["type"], row["expire_date"]):
        conn.execute("UPDATE license_keys SET status='expired',updated_at=? WHERE id=?", (iso_utc(), row["id"]))
        raise ValueError("授权已到期")
    device = upsert_device(conn, row, machine_id, data)
    conn.execute("UPDATE license_keys SET status='active',machine_id=?,updated_at=? WHERE id=?", (machine_id, iso_utc(), row["id"]))
    row = conn.execute("SELECT * FROM license_keys WHERE id=?", (row["id"],)).fetchone()
    return row, device


def create_card_keys(conn: sqlite3.Connection, kind: str, count: int, customer_id: str = "", max_devices: int = 1) -> list[str]:
    if kind not in VALID_TYPES:
        raise ValueError("授权类型无效")
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    result: list[str] = []
    for _ in range(max(1, min(1000, int(count)))):
        while True:
            key = "MX-" + "-".join("".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(4))
            try:
                stamp = iso_utc()
                conn.execute(
                    """INSERT INTO license_keys
                       (id,key_hash,key_hint,type,duration_days,status,customer_id,max_devices,created_at,updated_at)
                       VALUES(?,?,?,?,?,'issued',?,?,?,?)""",
                    (str(uuid.uuid4()), key_digest(key), key[-5:], kind, VALID_TYPES[kind], customer_id,
                     max(1, min(20, int(max_devices))), stamp, stamp),
                )
                result.append(key)
                break
            except sqlite3.IntegrityError:
                continue
    return result


_rate_lock = threading.Lock()
_rate: dict[str, list[float]] = {}


@app.before_request
def rate_limit():
    key = request_ip() + ":" + ("login" if request.path == "/admin/login" else "api")
    limit = 12 if request.path == "/admin/login" else 120
    now = time.monotonic()
    with _rate_lock:
        recent = [stamp for stamp in _rate.get(key, []) if now - stamp < 60]
        if len(recent) >= limit:
            if request.path.startswith("/api/"):
                return jsonify(ok=False, error="请求过于频繁"), 429
            return "Too many requests", 429
        recent.append(now)
        _rate[key] = recent
    return None


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.errorhandler(Exception)
def handle_error(exc: Exception):
    if isinstance(exc, HTTPException):
        return exc
    if request.path.startswith("/api/"):
        if isinstance(exc, ValueError):
            try:
                data = request.get_json(silent=True) or {}
                with connect() as conn:
                    row = None
                    if data.get("key_code"):
                        row = conn.execute("SELECT * FROM license_keys WHERE key_hash=?", (key_digest(str(data["key_code"])),)).fetchone()
                    log_event(conn, request.path.rsplit("/", 1)[-1], "denied", row=row,
                              machine_id=str(data.get("machine_id") or data.get("source_machine_id") or ""),
                              app_version=str(data.get("app_version") or ""), message=str(exc))
            except Exception:
                app.logger.exception("failed to write denied activation log")
            return jsonify(ok=False, error=str(exc)), 400
        app.logger.exception("unhandled API error")
        return jsonify(ok=False, error="服务器内部错误"), 500
    app.logger.exception("unhandled admin error")
    return "服务器内部错误", 500


@app.get("/health")
def health():
    return jsonify(ok=True, product=PRODUCT_ID, time=iso_utc())


@app.post("/api/license/activate")
@app.post("/api/v1/licenses/activate")
def activate():
    data = require_payload()
    machine_id = valid_machine(data.get("machine_id"))
    app_version = str(data.get("app_version") or "")
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM license_keys WHERE key_hash=?", (key_digest(str(data.get("key_code") or "")),)).fetchone()
        if not row or row["status"] in {"revoked", "disabled", "expired", "frozen"}:
            log_event(conn, "activate", "denied", row=row, machine_id=machine_id, app_version=app_version, message="invalid_or_frozen")
            raise ValueError("卡密无效、已冻结或已停用")
        if expiry_reached(row["type"], row["expire_date"]):
            conn.execute("UPDATE license_keys SET status='expired',updated_at=? WHERE id=?", (iso_utc(), row["id"]))
            log_event(conn, "activate", "denied", row=row, machine_id=machine_id, app_version=app_version, message="expired")
            raise ValueError("授权已到期")
        activated_at = row["activated_at"] or iso_utc()
        expire_date = row["expire_date"]
        if row["type"] != "permanent" and not expire_date:
            expire_date = calculate_expiry(row["type"], activated_at, int(row["duration_days"]))
        conn.execute(
            "UPDATE license_keys SET status='active',activated_at=?,expire_date=?,updated_at=? WHERE id=?",
            (activated_at, expire_date, iso_utc(), row["id"]),
        )
        row = conn.execute("SELECT * FROM license_keys WHERE id=?", (row["id"],)).fetchone()
        device = upsert_device(conn, row, machine_id, data)
        conn.execute("UPDATE license_keys SET machine_id=? WHERE id=?", (machine_id, row["id"]))
        row = conn.execute("SELECT * FROM license_keys WHERE id=?", (row["id"],)).fetchone()
        log_event(conn, "activate", "success", row=row, device_id=device["id"], machine_id=machine_id, app_version=app_version)
        token = issue_token(row, machine_id)
    return jsonify(ok=True, data={"status": "active", "license_token": token, "type": row["type"],
                                  "activated_at": row["activated_at"], "expire_date": row["expire_date"]})


@app.post("/api/license/check")
@app.post("/api/v1/licenses/verify")
def check_license_api():
    data = require_payload()
    machine_id = valid_machine(data.get("machine_id"))
    app_version = str(data.get("app_version") or "")
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row, device = active_row_for_token(conn, data, machine_id)
        log_event(conn, "check", "success", row=row, device_id=device["id"], machine_id=machine_id, app_version=app_version)
        token = issue_token(row, machine_id)
    return jsonify(ok=True, data={"status": "active", "license_token": token, "type": row["type"],
                                  "activated_at": row["activated_at"], "expire_date": row["expire_date"]})


@app.post("/api/license/deactivate")
@app.post("/api/v1/licenses/deactivate")
def deactivate():
    data = require_payload()
    machine_id = valid_machine(data.get("machine_id"))
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row, device = active_row_for_token(conn, data, machine_id)
        if int(row["unbind_count"]) >= int(row["max_unbinds"]):
            log_event(conn, "deactivate", "denied", row=row, device_id=device["id"], machine_id=machine_id, message="unbind_limit")
            raise ValueError("该授权已达到解绑次数上限")
        conn.execute("UPDATE devices SET status='unbound',unbound_at=?,last_seen_at=? WHERE id=?", (iso_utc(), iso_utc(), device["id"]))
        remaining = conn.execute("SELECT COUNT(*) FROM devices WHERE license_id=? AND status='active'", (row["id"],)).fetchone()[0]
        conn.execute(
            "UPDATE license_keys SET status=?,machine_id=?,unbind_count=unbind_count+1,updated_at=? WHERE id=?",
            ("active" if remaining else "issued", "" if not remaining else row["machine_id"], iso_utc(), row["id"]),
        )
        log_event(conn, "deactivate", "success", row=row, device_id=device["id"], machine_id=machine_id)
    return jsonify(ok=True, data={"status": "deactivated"})


@app.post("/api/v1/licenses/transfer")
def transfer():
    data = require_payload()
    source = valid_machine(data.get("source_machine_id"))
    target = valid_machine(data.get("target_machine_id"))
    if source == target:
        raise ValueError("目标机器不能与原机器相同")
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row, device = active_row_for_token(conn, data, source)
        if int(row["unbind_count"]) >= int(row["max_unbinds"]):
            raise ValueError("该授权已达到换机次数上限")
        conn.execute("UPDATE devices SET status='unbound',unbound_at=? WHERE id=?", (iso_utc(), device["id"]))
        target_device = upsert_device(conn, row, target, data)
        conn.execute("UPDATE license_keys SET machine_id=?,unbind_count=unbind_count+1,updated_at=? WHERE id=?", (target, iso_utc(), row["id"]))
        row = conn.execute("SELECT * FROM license_keys WHERE id=?", (row["id"],)).fetchone()
        log_event(conn, "transfer", "success", row=row, device_id=target_device["id"], machine_id=target, message=f"from={source}")
        token = issue_token(row, target)
    return jsonify(ok=True, data={"status": "active", "license_token": token})


def csrf_token() -> str:
    if not session.get("csrf"):
        session["csrf"] = secrets.token_urlsafe(24)
    return str(session["csrf"])


app.jinja_env.globals["csrf_token"] = csrf_token
app.jinja_env.globals["license_type_label"] = lambda value: TYPE_LABELS.get(str(value), str(value))


def require_csrf() -> None:
    if not hmac.compare_digest(str(session.get("csrf") or ""), str(request.form.get("csrf_token") or "")):
        raise ValueError("请求校验失败，请刷新页面重试")


def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_id"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        require_csrf()
        username = str(request.form.get("username") or "").strip()
        password = str(request.form.get("password") or "")
        with connect() as conn:
            user = conn.execute("SELECT * FROM admin_users WHERE username=? AND status='active'", (username,)).fetchone()
            if user and check_password_hash(user["password_hash"], password):
                session.clear()
                session.permanent = True
                session["admin_id"] = user["id"]
                session["username"] = user["username"]
                session["csrf"] = secrets.token_urlsafe(24)
                conn.execute("UPDATE admin_users SET last_login_at=?,updated_at=? WHERE id=?", (iso_utc(), iso_utc(), user["id"]))
                return redirect(url_for("admin_dashboard"))
        flash("账号或密码错误", "error")
    return render_template("login.html")


@app.post("/admin/logout")
@admin_required
def admin_logout():
    require_csrf()
    session.clear()
    return redirect(url_for("admin_login"))


def dashboard_data() -> dict[str, Any]:
    with connect() as conn:
        counts = {
            "customers": conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
            "licenses": conn.execute("SELECT COUNT(*) FROM license_keys").fetchone()[0],
            "active": conn.execute("SELECT COUNT(*) FROM license_keys WHERE status='active'").fetchone()[0],
            "expired": conn.execute("SELECT COUNT(*) FROM license_keys WHERE status='expired'").fetchone()[0],
            "frozen": conn.execute("SELECT COUNT(*) FROM license_keys WHERE status='frozen'").fetchone()[0],
            "devices": conn.execute("SELECT COUNT(*) FROM devices WHERE status='active'").fetchone()[0],
        }
        customers = conn.execute("SELECT * FROM customers ORDER BY created_at DESC LIMIT 100").fetchall()
        licenses = conn.execute(
            """SELECT l.*,c.name customer_name,
               (SELECT COUNT(*) FROM devices d WHERE d.license_id=l.id AND d.status='active') active_devices
               FROM license_keys l LEFT JOIN customers c ON c.id=l.customer_id ORDER BY l.created_at DESC LIMIT 200"""
        ).fetchall()
        devices = conn.execute(
            """SELECT d.*,l.key_hint,l.type,l.status license_status,c.name customer_name
               FROM devices d JOIN license_keys l ON l.id=d.license_id
               LEFT JOIN customers c ON c.id=l.customer_id ORDER BY d.last_seen_at DESC LIMIT 200"""
        ).fetchall()
        logs = conn.execute(
            """SELECT a.*,l.key_hint,c.name customer_name FROM activation_logs a
               LEFT JOIN license_keys l ON l.id=a.license_id LEFT JOIN customers c ON c.id=a.customer_id
               ORDER BY a.id DESC LIMIT 300"""
        ).fetchall()
    return {"counts": counts, "customers": customers, "licenses": licenses, "devices": devices, "logs": logs}


@app.get("/admin")
@admin_required
def admin_dashboard():
    return render_template("admin.html", generated_keys=[], **dashboard_data())


@app.post("/admin/customers/create")
@admin_required
def admin_customer_create():
    require_csrf()
    name = str(request.form.get("name") or "").strip()
    if not name:
        raise ValueError("客户名称不能为空")
    stamp = iso_utc()
    with connect() as conn:
        conn.execute(
            "INSERT INTO customers(id,name,company,phone,email,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), name[:100], str(request.form.get("company") or "")[:150],
             str(request.form.get("phone") or "")[:50], str(request.form.get("email") or "")[:150],
             str(request.form.get("notes") or "")[:500], stamp, stamp),
        )
    flash("客户已创建", "success")
    return redirect(url_for("admin_dashboard") + "#customers")


@app.post("/admin/licenses/generate")
@admin_required
def admin_license_generate():
    require_csrf()
    kind = str(request.form.get("type") or "month")
    if kind not in VALID_TYPES:
        raise ValueError("授权类型无效")
    count = max(1, min(100, int(request.form.get("count") or 1)))
    max_devices = max(1, min(20, int(request.form.get("max_devices") or 1)))
    customer_id = str(request.form.get("customer_id") or "")
    with connect() as conn:
        if customer_id and not conn.execute("SELECT 1 FROM customers WHERE id=?", (customer_id,)).fetchone():
            raise ValueError("客户不存在")
        keys = create_card_keys(conn, kind, count, customer_id, max_devices)
    return render_template("admin.html", generated_keys=keys, **dashboard_data())


@app.post("/admin/licenses/<license_id>/status")
@admin_required
def admin_license_status(license_id: str):
    require_csrf()
    status = str(request.form.get("status") or "")
    if status not in {"active", "issued", "frozen", "revoked"}:
        raise ValueError("授权状态无效")
    with connect() as conn:
        if conn.execute("UPDATE license_keys SET status=?,updated_at=? WHERE id=?", (status, iso_utc(), license_id)).rowcount != 1:
            raise ValueError("授权不存在")
    flash("授权状态已更新", "success")
    return redirect(url_for("admin_dashboard") + "#licenses")


@app.post("/admin/devices/<device_id>/unbind")
@admin_required
def admin_device_unbind(device_id: str):
    require_csrf()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        device = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if not device:
            raise ValueError("设备不存在")
        conn.execute("UPDATE devices SET status='unbound',unbound_at=? WHERE id=?", (iso_utc(), device_id))
        active_count = conn.execute("SELECT COUNT(*) FROM devices WHERE license_id=? AND status='active'", (device["license_id"],)).fetchone()[0]
        if not active_count:
            conn.execute("UPDATE license_keys SET status='issued',machine_id='',unbind_count=unbind_count+1,updated_at=? WHERE id=?", (iso_utc(), device["license_id"]))
    flash("设备已解绑", "success")
    return redirect(url_for("admin_dashboard") + "#devices")


def set_admin_password(username: str, password: str) -> None:
    if len(password) < 12:
        raise ValueError("管理员密码至少12位")
    stamp = iso_utc()
    with connect() as conn:
        existing = conn.execute("SELECT id FROM admin_users WHERE username=?", (username,)).fetchone()
        if existing:
            conn.execute("UPDATE admin_users SET password_hash=?,status='active',updated_at=? WHERE id=?", (generate_password_hash(password), stamp, existing["id"]))
        else:
            conn.execute("INSERT INTO admin_users(id,username,password_hash,created_at,updated_at) VALUES(?,?,?,?,?)", (str(uuid.uuid4()), username, generate_password_hash(password), stamp, stamp))


init_db()
