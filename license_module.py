"""Signed desktop license verification.

Trust model:
* the client contains only an Ed25519 public verification key;
* the issuer/server owns the private signing key;
* license.dat contains a signed entitlement, never an unsigned decision;
* SQLite is an operational cache and is not the source of license truth.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import socket
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from db import connect, now
from runtime_paths import CONFIG_DIR, DATA_DIR


PRODUCT_ID = "mixcut-ai-windows"
APP_VERSION = "1.3.4"
TOKEN_PREFIX = "MXLIC1"
VALID_TYPES = {"month", "year", "permanent"}
PUBLIC_KEY_B64 = "FS/YNzoWyelZRmfTJq7yZxFYnsjY8mlMha6XHofb4Qk="
LICENSE_FILE = DATA_DIR / "license.dat"
CLOCK_FILE = DATA_DIR / "license_clock.json"
CLOCK_ROLLBACK_TOLERANCE = timedelta(minutes=5)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def machine_id() -> str:
    """Return a stable privacy-preserving machine fingerprint."""
    parts = [platform.system(), platform.machine(), socket.gethostname()]
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            parts.append(str(winreg.QueryValueEx(key, "MachineGuid")[0]))
    except (OSError, ImportError):
        parts.extend([platform.node(), os.environ.get("COMPUTERNAME", "")])
    digest = hashlib.sha256("|".join(parts).encode("utf-8", "ignore")).hexdigest().upper()
    return "MX-" + "-".join(digest[index:index + 4] for index in range(0, 24, 4))


def normalize_key(value: str) -> str:
    value = str(value or "").strip()
    if value.startswith(TOKEN_PREFIX + "."):
        return value
    return "-".join(part for part in value.upper().replace(" ", "").split("-") if part)


def verify_signed_token(token: str) -> dict[str, Any]:
    try:
        prefix, payload_part, signature_part = str(token or "").strip().split(".", 2)
        if prefix != TOKEN_PREFIX:
            raise ValueError("授权凭证版本不受支持")
        payload_bytes = _b64url_decode(payload_part)
        signature = _b64url_decode(signature_part)
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBLIC_KEY_B64))
        public_key.verify(signature, payload_bytes)
        payload = json.loads(payload_bytes.decode("utf-8"))
    except InvalidSignature as exc:
        raise ValueError("授权签名校验失败") from exc
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("授权"):
            raise
        raise ValueError("授权凭证格式无效") from exc
    if _canonical_json(payload) != payload_bytes:
        raise ValueError("授权载荷不是规范格式")
    required = {"schema", "license_id", "product_id", "type", "machine_id", "issued_at", "nonce"}
    if not required.issubset(payload):
        raise ValueError("授权凭证字段不完整")
    return payload


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def save_license_token(token: str) -> dict[str, Any]:
    payload = verify_signed_token(token)
    _atomic_write_json(LICENSE_FILE, {"format": TOKEN_PREFIX, "token": token})
    return payload


def load_license_token() -> str:
    try:
        data = json.loads(LICENSE_FILE.read_text(encoding="utf-8"))
        return str(data.get("token") or "")
    except (OSError, ValueError, TypeError):
        return ""


def _settings() -> dict[str, str]:
    with connect() as conn:
        values = {row["key"]: row["value"] for row in conn.execute("SELECT key,value FROM app_settings")}
    config_path = CONFIG_DIR / "default_config.json"
    try:
        file_values = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    except (OSError, ValueError):
        file_values = {}
    for key, value in file_values.items():
        if key not in values or values[key] in (None, ""):
            values[key] = str(value)
    return values


def _read_clock_state() -> dict[str, str]:
    state: dict[str, str] = {}
    try:
        state.update(json.loads(CLOCK_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        pass
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\MixcutAI\License") as key:
            registry_seen = str(winreg.QueryValueEx(key, "LastSeenUtc")[0])
            if not state.get("last_seen_utc") or _parse_utc(registry_seen) > _parse_utc(state["last_seen_utc"]):
                state["last_seen_utc"] = registry_seen
    except (OSError, ImportError, ValueError):
        pass
    return state


def _write_clock_state(state: dict[str, str]) -> None:
    _atomic_write_json(CLOCK_FILE, state)
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\MixcutAI\License") as key:
            winreg.SetValueEx(key, "LastSeenUtc", 0, winreg.REG_SZ, state.get("last_seen_utc", ""))
    except (OSError, ImportError):
        pass


def _post_json(url: str, payload: dict[str, Any], timeout: int = 12) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")
        raise ValueError(f"授权服务器返回 HTTP {exc.code}: {detail[:180]}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ConnectionError(f"无法连接授权服务器: {exc}") from exc
    if not result.get("ok", True):
        raise ValueError(str(result.get("error") or result.get("message") or "授权请求失败"))
    return result.get("data", result)


def _device_metadata() -> dict[str, str]:
    return {
        "device_name": socket.gethostname()[:120],
        "os_info": platform.platform()[:200],
    }


def _post_json_compatible(server_url: str, new_path: str, legacy_path: str,
                          payload: dict[str, Any], timeout: int = 12) -> dict[str, Any]:
    try:
        return _post_json(server_url.rstrip("/") + new_path, payload, timeout)
    except ValueError as exc:
        if "HTTP 404" not in str(exc):
            raise
    return _post_json(server_url.rstrip("/") + legacy_path, payload, timeout)


def _server_activate(server_url: str, key_code: str, current_machine_id: str) -> str:
    data = _post_json_compatible(server_url, "/api/license/activate", "/api/v1/licenses/activate", {
        "key_code": key_code,
        "machine_id": current_machine_id,
        "product_id": PRODUCT_ID,
        "app_version": APP_VERSION,
        **_device_metadata(),
    })
    token = str(data.get("license_token") or "")
    if not token:
        raise ValueError("授权服务器未返回签名凭证")
    return token


def _server_verify(server_url: str, token: str, current_machine_id: str) -> str:
    data = _post_json_compatible(server_url, "/api/license/check", "/api/v1/licenses/verify", {
        "license_token": token,
        "machine_id": current_machine_id,
        "product_id": PRODUCT_ID,
        "app_version": APP_VERSION,
        **_device_metadata(),
    }, timeout=8)
    if str(data.get("status") or "active") != "active":
        raise ValueError("授权已被服务器停用或吊销")
    return str(data.get("license_token") or token)


@dataclass(frozen=True)
class LicenseState:
    valid: bool
    status: str
    message: str
    key_code: str = ""
    license_type: str = ""
    expire_date: str = ""
    machine_id: str = ""
    days_left: int | None = None


def _cache_payload(payload: dict[str, Any], token: str, status: str = "active", server_checked: bool = False) -> None:
    timestamp = now()
    with connect() as conn:
        conn.execute(
            """INSERT INTO license_keys(key_code,type,expire_date,machine_id,status,server_token,activated_at,last_check_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key_code) DO UPDATE SET type=excluded.type,expire_date=excluded.expire_date,
               machine_id=excluded.machine_id,status=excluded.status,server_token=excluded.server_token,
               last_check_at=excluded.last_check_at,updated_at=excluded.updated_at""",
            (str(payload["license_id"]), str(payload["type"]), str(payload.get("expire_date") or ""),
             str(payload["machine_id"]), status, token, timestamp,
             timestamp if server_checked else "", timestamp, timestamp),
        )


def check_license(current_machine_id: str | None = None, current_time: datetime | None = None,
                  allow_online: bool = True) -> LicenseState:
    current_machine_id = current_machine_id or machine_id()
    current_time = (current_time or _utc_now()).astimezone(timezone.utc)
    token = load_license_token()
    if not token:
        return LicenseState(False, "missing", "软件尚未激活", machine_id=current_machine_id)
    try:
        payload = verify_signed_token(token)
    except ValueError as exc:
        return LicenseState(False, "invalid_signature", str(exc), machine_id=current_machine_id)
    license_id = str(payload["license_id"])
    license_type = str(payload["type"])
    expire_date = str(payload.get("expire_date") or "")
    if payload.get("product_id") != PRODUCT_ID or license_type not in VALID_TYPES:
        return LicenseState(False, "invalid_product", "授权不适用于当前产品", license_id, license_type, expire_date, current_machine_id)
    if str(payload.get("machine_id")) != current_machine_id:
        return LicenseState(False, "machine_mismatch", "授权已绑定其他机器", license_id, license_type, expire_date, current_machine_id)
    try:
        issued_at = _parse_utc(str(payload["issued_at"]))
    except ValueError:
        return LicenseState(False, "invalid", "授权签发时间无效", license_id, license_type, expire_date, current_machine_id)
    if current_time + CLOCK_ROLLBACK_TOLERANCE < issued_at:
        return LicenseState(False, "clock_invalid", "系统时间早于授权签发时间，请联网校准", license_id, license_type, expire_date, current_machine_id)

    clock_state = _read_clock_state()
    try:
        last_seen = _parse_utc(clock_state.get("last_seen_utc", ""))
    except ValueError:
        last_seen = None
    if last_seen and current_time + CLOCK_ROLLBACK_TOLERANCE < last_seen:
        return LicenseState(False, "clock_rollback", "检测到系统时间回拨，请联网重新验证", license_id, license_type, expire_date, current_machine_id)

    days_left: int | None = None
    if license_type != "permanent":
        try:
            expiry = date.fromisoformat(expire_date)
        except ValueError:
            return LicenseState(False, "invalid", "签名授权缺少有效期", license_id, license_type, expire_date, current_machine_id)
        days_left = (expiry - current_time.date()).days
        if days_left < 0:
            _cache_payload(payload, token, "expired")
            return LicenseState(False, "expired", "授权已到期", license_id, license_type, expire_date, current_machine_id, days_left)

    cfg = _settings()
    server_url = str(cfg.get("license_server_url") or "").strip()
    server_checked = False
    if server_url and allow_online:
        try:
            refreshed_token = _server_verify(server_url, token, current_machine_id)
            refreshed_payload = verify_signed_token(refreshed_token)
            if str(refreshed_payload.get("machine_id")) != current_machine_id:
                raise ValueError("服务器返回的授权机器码不匹配")
            token = refreshed_token; payload = refreshed_payload
            save_license_token(token)
            clock_state["last_server_check_utc"] = _iso_utc(current_time)
            server_checked = True
        except ConnectionError:
            try:
                last_server = _parse_utc(clock_state.get("last_server_check_utc", ""))
            except ValueError:
                last_server = None
            grace_days = max(0, int(cfg.get("license_offline_grace_days") or 3))
            if license_type != "permanent" and last_server and current_time - last_server > timedelta(days=grace_days):
                return LicenseState(False, "offline_grace_expired", "离线授权宽限期已结束，请联网验证", license_id, license_type, expire_date, current_machine_id, days_left)
        except ValueError as exc:
            _cache_payload(payload, token, "revoked")
            return LicenseState(False, "revoked", str(exc), license_id, license_type, expire_date, current_machine_id, days_left)

    if not last_seen or current_time > last_seen:
        clock_state["last_seen_utc"] = _iso_utc(current_time)
    _write_clock_state(clock_state)
    _cache_payload(payload, token, "active", server_checked)
    message = "永久授权" if license_type == "permanent" else f"授权剩余 {days_left} 天"
    return LicenseState(True, "active", message, license_id, license_type, expire_date, current_machine_id, days_left)


def activate_license(key_code: str, current_machine_id: str | None = None) -> LicenseState:
    key_code = normalize_key(key_code)
    current_machine_id = current_machine_id or machine_id()
    cfg = _settings()
    server_url = str(cfg.get("license_server_url") or "").strip()
    if key_code.startswith(TOKEN_PREFIX + "."):
        token = key_code
    else:
        if len(key_code.replace("-", "")) < 12:
            raise ValueError("卡密格式不正确")
        if not server_url:
            raise ValueError("短卡密需要连接授权服务器")
        token = _server_activate(server_url, key_code, current_machine_id)
        clock_state = _read_clock_state()
        clock_state["last_server_check_utc"] = _iso_utc(_utc_now())
        clock_state["last_seen_utc"] = _iso_utc(_utc_now())
        _write_clock_state(clock_state)
    payload = verify_signed_token(token)
    if str(payload.get("machine_id")) != current_machine_id:
        raise ValueError("授权已绑定其他机器")
    if payload.get("product_id") != PRODUCT_ID:
        raise ValueError("授权不适用于当前产品")
    save_license_token(token)
    state = check_license(current_machine_id, allow_online=False)
    if not state.valid:
        raise ValueError(state.message)
    return state


def request_deactivate() -> dict[str, Any]:
    cfg = _settings(); server_url = str(cfg.get("license_server_url") or "").strip()
    if not server_url:
        raise ValueError("未配置授权服务器")
    token = load_license_token()
    return _post_json_compatible(server_url, "/api/license/deactivate", "/api/v1/licenses/deactivate", {
        "license_token": token, "machine_id": machine_id(), "product_id": PRODUCT_ID,
        "app_version": APP_VERSION, **_device_metadata(),
    })


def request_transfer(target_machine_id: str) -> dict[str, Any]:
    cfg = _settings(); server_url = str(cfg.get("license_server_url") or "").strip()
    if not server_url:
        raise ValueError("未配置授权服务器")
    return _post_json(server_url.rstrip("/") + "/api/v1/licenses/transfer", {
        "license_token": load_license_token(), "source_machine_id": machine_id(),
        "target_machine_id": target_machine_id, "product_id": PRODUCT_ID,
    })


def license_summary() -> dict[str, Any]:
    state = check_license()
    return {"valid": state.valid, "status": state.status, "message": state.message,
            "type": state.license_type, "expire_date": state.expire_date,
            "machine_id": state.machine_id, "days_left": state.days_left}



