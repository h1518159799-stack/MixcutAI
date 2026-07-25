"""OAuth authorization orchestration shared by API platform adapters."""

from __future__ import annotations

import json
import os
import webbrowser
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from db import connect, now
from oauth_token_store import protect, unprotect
from oauth_callback_server import ensure_callback_server
from platforms import get_adapter


def _config(account: dict, platform: dict) -> dict:
    try:
        config = json.loads(account.get("api_config_json") or "{}")
    except (TypeError, ValueError):
        config = {}
    code = str(platform["code"]).upper()
    config["client_id"] = str(config.get("client_id") or os.getenv(f"MIXCUT_{code}_CLIENT_ID", "")).strip()
    encrypted_secret = str(config.get("client_secret_encrypted") or "").strip()
    try:
        stored_secret = unprotect(encrypted_secret) if encrypted_secret else ""
    except Exception:
        stored_secret = ""
    config["client_secret"] = str(stored_secret or os.getenv(f"MIXCUT_{code}_CLIENT_SECRET", "")).strip()
    config["redirect_uri"] = str(config.get("redirect_uri") or os.getenv("MIXCUT_OAUTH_REDIRECT_URI", "http://127.0.0.1:17866/api/oauth/callback")).strip()
    return config


def _oauth_error_message(exc: Exception) -> str:
    raw = str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = str(exc)
    lowered = raw.lower()
    mappings = (
        ("access_denied", "用户取消了 Google OAuth 授权"),
        ("redirect_uri_mismatch", "Google OAuth 回调地址不匹配，请检查桌面应用凭据"),
        ("invalid_client", "OAuth Client ID 或 Client Secret 无效"),
        ("invalid_grant", "授权码或 Refresh Token 已失效，请重新授权"),
        ("insufficient", "YouTube 授权权限不足，请重新授权并勾选全部权限"),
    )
    for marker, message in mappings:
        if marker in lowered:
            return message
    return "Google OAuth 授权失败：" + raw[:500]


def _youtube_channel(access_token: str) -> tuple[str, str]:
    url = "https://www.googleapis.com/youtube/v3/channels?" + urllib.parse.urlencode({
        "part": "id,snippet", "mine": "true",
    })
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {access_token}", "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(_oauth_error_message(exc)) from exc
    items = payload.get("items") or []
    if not items:
        raise RuntimeError("当前 Google 账号没有可用的 YouTube 频道")
    channel = items[0]
    return str(channel.get("id") or ""), str((channel.get("snippet") or {}).get("title") or "")


def authorization_status(account_id: int) -> dict:
    with connect() as conn:
        token = conn.execute("SELECT expires_at,status,updated_at FROM oauth_tokens WHERE account_id=?", (account_id,)).fetchone()
    if not token:
        return {"status": "not_authorized", "expires_at": ""}
    expired = bool(token["expires_at"] and token["expires_at"] <= now())
    return {"status": "expired" if expired else token["status"], "expires_at": token["expires_at"]}


def cancel_authorization(state: str, error: str = "access_denied") -> str:
    message = _oauth_error_message(RuntimeError(error or "access_denied"))
    with connect() as conn:
        session = conn.execute("SELECT account_id FROM oauth_sessions WHERE state=?", (state,)).fetchone()
        if session:
            conn.execute(
                "UPDATE publish_accounts SET authorization_status='not_authorized',last_error=?,updated_at=? WHERE id=?",
                (message, now(), session["account_id"]),
            )
            conn.execute("DELETE FROM oauth_sessions WHERE state=?", (state,))
    return message


def start_authorization(platform: dict, account: dict) -> dict:
    adapter = get_adapter(platform["adapter_code"] or platform["code"])
    if adapter.integration_type != "api":
        raise ValueError("当前账号不是 API 授权类型")
    config = _config(account, platform)
    if not config["client_id"]:
        return {"status": "configure_required", "message": f"请先配置 {platform['name']} OAuth Client ID"}
    if platform["code"] == "youtube" and not config["client_secret"]:
        return {"status": "configure_required", "message": "请先配置 YouTube OAuth Client Secret"}
    auth = adapter.authorization_request(config["client_id"], config["redirect_uri"])
    ensure_callback_server(config["redirect_uri"])
    expires = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        conn.execute("DELETE FROM oauth_sessions WHERE account_id=?", (account["id"],))
        conn.execute(
            "INSERT INTO oauth_sessions(state,account_id,code_verifier,redirect_uri,expires_at,created_at) VALUES(?,?,?,?,?,?)",
            (auth.state, account["id"], auth.code_verifier, config["redirect_uri"], expires, now()),
        )
        conn.execute("UPDATE publish_accounts SET authorization_status='authorizing',updated_at=? WHERE id=?", (now(), account["id"]))
    webbrowser.open(auth.url, new=1)
    return {"status": "authorizing", "authorization_url": auth.url, "expires_at": expires}


def complete_authorization(state: str, code: str) -> dict:
    with connect() as conn:
        session = conn.execute("SELECT * FROM oauth_sessions WHERE state=?", (state,)).fetchone()
        if not session or session["expires_at"] <= now():
            raise ValueError("OAuth 授权会话无效或已过期")
        account = conn.execute("SELECT * FROM publish_accounts WHERE id=?", (session["account_id"],)).fetchone()
        platform = conn.execute("SELECT * FROM platforms WHERE id=?", (account["platform_id"],)).fetchone()
    account, platform = dict(account), dict(platform)
    adapter = get_adapter(platform["adapter_code"] or platform["code"])
    config = _config(account, platform)
    try:
        token = adapter.exchange_token(
            code=code, client_id=config["client_id"], client_secret=config["client_secret"],
            redirect_uri=session["redirect_uri"], code_verifier=session["code_verifier"],
        )
    except Exception as exc:
        message = _oauth_error_message(exc)
        with connect() as conn:
            conn.execute(
                "UPDATE publish_accounts SET authorization_status='not_authorized',last_error=?,updated_at=? WHERE id=?",
                (message, now(), account["id"]),
            )
        raise RuntimeError(message) from exc
    channel_id = channel_title = ""
    if platform["code"] == "youtube":
        channel_id, channel_title = _youtube_channel(str(token["access_token"]))
    expires_at = ""
    if token.get("expires_in"):
        expires_at = (datetime.now() + timedelta(seconds=int(token["expires_in"]))).strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        conn.execute(
            """INSERT INTO oauth_tokens(account_id,provider,access_token_encrypted,refresh_token_encrypted,
               token_type,scope,expires_at,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(account_id) DO UPDATE SET access_token_encrypted=excluded.access_token_encrypted,
               refresh_token_encrypted=CASE WHEN excluded.refresh_token_encrypted='' THEN oauth_tokens.refresh_token_encrypted ELSE excluded.refresh_token_encrypted END,
               token_type=excluded.token_type,scope=excluded.scope,expires_at=excluded.expires_at,status='authorized',updated_at=excluded.updated_at""",
            (account["id"], platform["code"], protect(token["access_token"]), protect(token.get("refresh_token", "")),
             token.get("token_type", "Bearer"), token.get("scope", ""), expires_at, "authorized", now(), now()),
        )
        conn.execute("DELETE FROM oauth_sessions WHERE state=?", (state,))
        conn.execute(
            """UPDATE publish_accounts SET authorization_status='authorized',channel_id=?,channel_title=?,
               last_checked_at=?,last_error='',updated_at=? WHERE id=?""",
            (channel_id, channel_title, now(), now(), account["id"]),
        )
    return {
        "status": "authorized", "platform": platform["name"], "account": account["account_name"],
        "channel_id": channel_id, "channel_title": channel_title,
    }
