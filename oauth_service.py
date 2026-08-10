"""Unified OAuth setup, authorization and account selection for API platforms."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from typing import Any

from db import connect, now
from oauth_token_store import protect, unprotect
from platforms import get_adapter


OAUTH_SESSION_MINUTES = 10
PROVIDERS: dict[str, dict[str, Any]] = {
    "youtube": {
        "platform_code": "youtube", "adapter": "youtube", "name": "YouTube",
        "callback_slug": "youtube", "requires_secret": True, "pkce": True,
    },
    "facebook_page": {
        "platform_code": "facebook", "adapter": "facebook", "name": "Facebook Page",
        "callback_slug": "facebook-page", "requires_secret": True, "pkce": False,
    },
    "instagram_business": {
        "platform_code": "instagram", "adapter": "instagram", "name": "Instagram Business",
        "callback_slug": "instagram-business", "requires_secret": True, "pkce": False,
    },
    "x": {
        "platform_code": "x", "adapter": "x", "name": "X",
        "callback_slug": "x", "requires_secret": False, "pkce": True,
    },
}
ALIASES = {
    "youtube": "youtube", "facebook": "facebook_page", "facebook-page": "facebook_page",
    "facebook_page": "facebook_page", "instagram": "instagram_business",
    "instagram-business": "instagram_business", "instagram_business": "instagram_business", "x": "x",
}


def normalize_provider(value: str) -> str:
    provider = ALIASES.get(str(value or "").strip().lower())
    if not provider or provider not in PROVIDERS:
        raise ValueError("不支持的 API 授权平台")
    return provider


def callback_uri(provider: str) -> str:
    item = PROVIDERS[normalize_provider(provider)]
    return f"http://127.0.0.1:17866/api/oauth/{item['callback_slug']}/callback"


def _decode_json(value: str, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return default


def _provider_config_row(provider: str) -> dict | None:
    with connect() as conn:
        value = conn.execute("SELECT * FROM oauth_provider_configs WHERE provider=?", (provider,)).fetchone()
    return dict(value) if value else None


def _legacy_config(provider: str, account: dict | None = None) -> dict:
    if not account:
        return {}
    raw = _decode_json(str(account.get("api_config_json") or "{}"), {})
    return raw if isinstance(raw, dict) else {}


def get_provider_config(provider: str, account: dict | None = None) -> dict:
    """Return internal runtime config. Never expose this object through an API."""
    provider = normalize_provider(provider)
    spec = PROVIDERS[provider]
    stored = _provider_config_row(provider) or {}
    if not stored and not account:
        # Read legacy per-account configuration once so existing users are not
        # forced to re-enter credentials after the provider-level migration.
        with connect() as conn:
            legacy_row = conn.execute(
                """SELECT a.* FROM publish_accounts a JOIN platforms p ON p.id=a.platform_id
                   WHERE p.code=? AND a.status='active' AND a.api_config_json<>'{}'
                   ORDER BY a.updated_at DESC LIMIT 1""",
                (spec["platform_code"],),
            ).fetchone()
        account = dict(legacy_row) if legacy_row else None
    legacy = _legacy_config(provider, account)
    client_id = str(stored.get("client_id") or legacy.get("client_id") or "").strip()
    secret_blob = str(stored.get("client_secret_encrypted") or legacy.get("client_secret_encrypted") or "").strip()
    try:
        client_secret = unprotect(secret_blob) if secret_blob else ""
    except Exception:
        client_secret = ""
    env_code = str(spec["platform_code"]).upper()
    client_id = client_id or os.getenv(f"MIXCUT_{env_code}_CLIENT_ID", "").strip()
    client_secret = client_secret or os.getenv(f"MIXCUT_{env_code}_CLIENT_SECRET", "").strip()
    extra = _decode_json(str(stored.get("extra_config_json") or "{}"), {})
    return {
        "provider": provider,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": callback_uri(provider),
        "extra": extra if isinstance(extra, dict) else {},
    }


def save_provider_config(provider: str, data: dict) -> dict:
    provider = normalize_provider(provider)
    spec = PROVIDERS[provider]
    client_id = str(data.get("client_id") or "").strip()
    supplied_secret = str(data.get("client_secret") or "").strip()
    existing = _provider_config_row(provider) or {}
    secret_blob = str(existing.get("client_secret_encrypted") or "")
    if supplied_secret:
        secret_blob = protect(supplied_secret)
    if not client_id:
        raise ValueError("请填写应用编号")
    if spec["requires_secret"] and not secret_blob:
        raise ValueError("请填写应用密钥")
    extra = {
        "source_name": str(data.get("source_name") or "").strip(),
        "oauth_mode": str(data.get("oauth_mode") or "authorization_code_pkce").strip(),
        "review_note": str(data.get("review_note") or "").strip(),
    }
    stamp = now()
    with connect() as conn:
        conn.execute(
            """INSERT INTO oauth_provider_configs(provider,client_id,client_secret_encrypted,extra_config_json,status,created_at,updated_at)
               VALUES(?,?,?,?, 'ready_to_authorize',?,?)
               ON CONFLICT(provider) DO UPDATE SET client_id=excluded.client_id,
               client_secret_encrypted=excluded.client_secret_encrypted,
               extra_config_json=excluded.extra_config_json,status='ready_to_authorize',updated_at=excluded.updated_at""",
            (provider, client_id, secret_blob, json.dumps(extra, ensure_ascii=False), stamp, stamp),
        )
    return provider_preflight(provider)


def import_google_oauth_payload(payload: dict, source_name: str = "") -> dict:
    config = payload.get("installed")
    if not isinstance(config, dict):
        raise ValueError("请选择 Google 桌面应用 OAuth JSON")
    client_id = str(config.get("client_id") or "").strip()
    client_secret = str(config.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise ValueError("Google OAuth JSON 缺少客户端配置")
    result = save_provider_config("youtube", {
        "client_id": client_id, "client_secret": client_secret, "source_name": source_name,
    })
    result["message"] = "Google OAuth 配置已导入"
    return result


def _platform_for_provider(provider: str) -> dict | None:
    code = PROVIDERS[provider]["platform_code"]
    with connect() as conn:
        value = conn.execute("SELECT * FROM platforms WHERE code=? AND enabled=1", (code,)).fetchone()
    return dict(value) if value else None


def provider_preflight(provider: str, account_id: int = 0) -> dict:
    provider = normalize_provider(provider)
    spec = PROVIDERS[provider]
    config = get_provider_config(provider)
    configured = bool(config["client_id"] and (config["client_secret"] or not spec["requires_secret"]))
    status = "ready_to_authorize" if configured else "not_configured"
    account = None
    token_status = "not_authorized"
    if account_id:
        with connect() as conn:
            value = conn.execute("SELECT * FROM publish_accounts WHERE id=? AND status='active'", (account_id,)).fetchone()
        account = dict(value) if value else None
        if not account:
            raise ValueError("发布账号不存在")
        token_status = authorization_status(account_id)["status"]
        status = token_status if token_status in {"authorized", "token_expired", "needs_reauthorization"} else status
    with connect() as conn:
        count = int(conn.execute(
            """SELECT COUNT(*) FROM publish_accounts a JOIN platforms p ON p.id=a.platform_id
               WHERE a.status='active' AND p.code=? AND a.authorization_status='authorized'""",
            (spec["platform_code"],),
        ).fetchone()[0])
    callback_ready = True
    callback_error = ""
    try:
        from oauth_callback_server import callback_server_status
        callback_state = callback_server_status(callback_uri(provider))
        callback_ready = bool(callback_state.get("available", True))
        callback_error = str(callback_state.get("message") or "")
    except Exception:
        callback_ready = True
    if not callback_ready and configured:
        status = "config_invalid"
    next_action = "configure" if not configured else ("repair_callback" if not callback_ready else "authorize")
    if token_status == "authorized":
        next_action = "check_or_reauthorize"
    elif token_status in {"token_expired", "needs_reauthorization"}:
        next_action = "reauthorize"
    return {
        "provider": provider,
        "platform_name": spec["name"],
        "status": status,
        "developer_configured": configured,
        "config_complete": configured,
        "can_generate_authorization_url": configured and callback_ready,
        "callback_uri": callback_uri(provider),
        "callback_service": "available" if callback_ready else "port_conflict",
        "callback_message": callback_error,
        "network_status": "unchecked",
        "authorized_account_count": count,
        "authorization_status": token_status,
        "next_action": next_action,
        "client_id_masked": "已配置" if config["client_id"] else "尚未配置",
        "secret_configured": bool(config["client_secret"]),
        "publication_capability": (_platform_for_provider(provider) or {}).get("capability_status", "untested"),
    }


def _oauth_error(exc: Exception) -> tuple[str, str]:
    raw = str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
    lowered = raw.lower()
    mappings = (
        ("access_denied", "authorization_cancelled", "用户取消了授权"),
        ("redirect_uri_mismatch", "redirect_uri_mismatch", "授权回调地址不一致"),
        ("invalid_client", "invalid_client", "应用编号或应用密钥无效"),
        ("invalid_scope", "invalid_scope", "当前权限配置不足"),
        ("invalid_grant", "invalid_grant", "授权结果已经过期，请重新授权"),
        ("rate", "rate_limited", "平台请求频率受限，请稍后重试"),
        ("timed out", "network_timeout", "无法访问平台授权服务"),
        ("urlopen", "network_error", "无法访问平台授权服务"),
    )
    for marker, code, message in mappings:
        if marker in lowered:
            return code, message
    return "authorization_failed", "账号授权失败，请检查平台配置后重试"


def _json_request(url: str, access_token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _discover_accounts(provider: str, token: dict) -> list[dict]:
    access_token = str(token.get("access_token") or "")
    if provider == "youtube":
        url = "https://www.googleapis.com/youtube/v3/channels?" + urllib.parse.urlencode({"part": "id,snippet", "mine": "true"})
        payload = _json_request(url, access_token)
        return [{
            "id": str(item.get("id") or ""),
            "name": str((item.get("snippet") or {}).get("title") or ""),
            "avatar": str(((item.get("snippet") or {}).get("thumbnails") or {}).get("default", {}).get("url") or ""),
            "kind": "YouTube频道",
        } for item in payload.get("items") or [] if item.get("id")]
    if provider == "facebook_page":
        payload = _json_request("https://graph.facebook.com/me/accounts?fields=id,name,picture,access_token,tasks", access_token)
        return [{
            "id": str(item.get("id") or ""), "name": str(item.get("name") or ""),
            "avatar": str((((item.get("picture") or {}).get("data") or {}).get("url")) or ""),
            "kind": "Facebook主页", "page_access_token": str(item.get("access_token") or ""),
            "permissions": item.get("tasks") or [],
        } for item in payload.get("data") or [] if item.get("id")]
    if provider == "instagram_business":
        payload = _json_request(
            "https://graph.facebook.com/me/accounts?fields=id,name,access_token,instagram_business_account{id,username,profile_picture_url,account_type}",
            access_token,
        )
        accounts = []
        for page in payload.get("data") or []:
            item = page.get("instagram_business_account") or {}
            if item.get("id"):
                accounts.append({
                    "id": str(item.get("id")), "name": str(item.get("username") or "Instagram专业账号"),
                    "avatar": str(item.get("profile_picture_url") or ""),
                    "kind": str(item.get("account_type") or "Instagram Business"),
                    "parent_page_id": str(page.get("id") or ""),
                    "page_access_token": str(page.get("access_token") or ""),
                })
        return accounts
    if provider == "x":
        payload = _json_request("https://api.x.com/2/users/me?user.fields=id,name,username,profile_image_url", access_token)
        item = payload.get("data") or {}
        return [{
            "id": str(item.get("id") or ""), "name": str(item.get("name") or item.get("username") or ""),
            "username": str(item.get("username") or ""), "avatar": str(item.get("profile_image_url") or ""),
            "kind": "X账号",
        }] if item.get("id") else []
    return []


def _session_public(row: dict) -> dict:
    candidates: list[dict] = []
    if row.get("result_encrypted"):
        try:
            pending = json.loads(unprotect(str(row["result_encrypted"])))
            for item in pending.get("candidates") or []:
                candidates.append({key: value for key, value in item.items() if key not in {"page_access_token"}})
        except Exception:
            candidates = []
    return {
        "state": row.get("state", ""), "provider": row.get("provider", ""),
        "status": row.get("status", "waiting_callback"), "authorization_url": row.get("authorization_url", ""),
        "expires_at": row.get("expires_at", ""), "error_code": row.get("error_code", ""),
        "message": row.get("error_message", ""), "candidates": candidates,
    }


def session_status(state: str) -> dict:
    with connect() as conn:
        value = conn.execute("SELECT * FROM oauth_sessions WHERE state=?", (state,)).fetchone()
    if not value:
        raise ValueError("授权会话不存在或已经结束")
    result = _session_public(dict(value))
    if str(value["expires_at"]) <= now() and result["status"] not in {"authorized", "authorization_cancelled", "authorization_failed"}:
        with connect() as conn:
            conn.execute("UPDATE oauth_sessions SET status='authorization_failed',error_code='session_expired',error_message='授权结果已经过期，请重新授权' WHERE state=?", (state,))
        result.update({"status": "authorization_failed", "error_code": "session_expired", "message": "授权结果已经过期，请重新授权"})
    return result


def start_authorization(platform: dict, account: dict) -> dict:
    provider = normalize_provider(str(platform.get("code") or ""))
    preflight = provider_preflight(provider, int(account["id"]))
    if not preflight["developer_configured"]:
        return {**preflight, "message": f"请先配置 {preflight['platform_name']} 平台接入"}
    if not preflight["can_generate_authorization_url"]:
        raise RuntimeError(preflight.get("callback_message") or "本机授权回调端口被占用")
    config = get_provider_config(provider, account)
    adapter = get_adapter(PROVIDERS[provider]["adapter"])
    auth = adapter.authorization_request(config["client_id"], config["redirect_uri"])
    try:
        from oauth_callback_server import ensure_callback_server
        ensure_callback_server(config["redirect_uri"])
    except OSError as exc:
        raise RuntimeError("本机授权回调端口被占用，请关闭占用17866端口的程序后重试") from exc
    expires = (datetime.now() + timedelta(minutes=OAUTH_SESSION_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    stamp = now()
    with connect() as conn:
        conn.execute("DELETE FROM oauth_sessions WHERE account_id=? AND status NOT IN ('authorized')", (account["id"],))
        conn.execute(
            """INSERT INTO oauth_sessions(state,account_id,code_verifier,redirect_uri,expires_at,created_at,
               provider,status,authorization_url,result_encrypted,error_code,error_message,completed_at,consumed_at)
               VALUES(?,?,?,?,?,?,?,'opening_browser',?,'','','','','')""",
            (auth.state, account["id"], auth.code_verifier, config["redirect_uri"], expires, stamp, provider, auth.url),
        )
        conn.execute("UPDATE publish_accounts SET authorization_status='authorizing',last_error='',updated_at=? WHERE id=?", (stamp, account["id"]))
    opened = bool(webbrowser.open(auth.url, new=1))
    status = "waiting_callback"
    with connect() as conn:
        conn.execute("UPDATE oauth_sessions SET status=? WHERE state=?", (status, auth.state))
    return {
        "status": status, "state": auth.state, "authorization_url": auth.url,
        "expires_at": expires, "browser_opened": opened,
        "message": "已打开系统浏览器，请完成平台授权" if opened else "未能自动打开浏览器，请复制授权链接后在浏览器中打开",
    }


def reopen_authorization(state: str) -> dict:
    session = session_status(state)
    opened = bool(webbrowser.open(session["authorization_url"], new=1))
    return {**session, "browser_opened": opened}


def cancel_authorization(state: str, error: str = "access_denied") -> str:
    code, message = _oauth_error(RuntimeError(error or "access_denied"))
    with connect() as conn:
        session = conn.execute("SELECT account_id FROM oauth_sessions WHERE state=?", (state,)).fetchone()
        if session:
            conn.execute("UPDATE oauth_sessions SET status='authorization_cancelled',error_code=?,error_message=?,completed_at=? WHERE state=?", (code, message, now(), state))
            conn.execute("UPDATE publish_accounts SET authorization_status='not_authorized',last_error=?,updated_at=? WHERE id=?", (message, now(), session["account_id"]))
    return message


def complete_authorization(state: str, code: str, provider_hint: str = "") -> dict:
    if not state or not code:
        raise ValueError("授权回调缺少必要参数")
    with connect() as conn:
        session_row = conn.execute("SELECT * FROM oauth_sessions WHERE state=?", (state,)).fetchone()
        if not session_row:
            raise ValueError("未知授权回调，已拒绝处理")
        session = dict(session_row)
        if provider_hint and normalize_provider(provider_hint) != session.get("provider"):
            raise ValueError("授权回调平台不匹配")
        if session["expires_at"] <= now():
            raise ValueError("授权结果已经过期，请重新授权")
        if session.get("consumed_at"):
            raise ValueError("该授权结果已经使用，不能重复提交")
        conn.execute("UPDATE oauth_sessions SET consumed_at=?,status='reading_accounts' WHERE state=?", (now(), state))
        account_row = conn.execute("SELECT * FROM publish_accounts WHERE id=? AND status='active'", (session["account_id"],)).fetchone()
        if not account_row:
            raise ValueError("授权对应的本地账号不存在")
        account = dict(account_row)
    provider = normalize_provider(session.get("provider") or provider_hint)
    config = get_provider_config(provider, account)
    adapter = get_adapter(PROVIDERS[provider]["adapter"])
    try:
        token = adapter.exchange_token(
            code=code, client_id=config["client_id"], client_secret=config["client_secret"],
            redirect_uri=session["redirect_uri"], code_verifier=session["code_verifier"],
        )
        candidates = _discover_accounts(provider, token)
        if not candidates:
            if provider == "instagram_business":
                raise RuntimeError("当前账号不是可用于API发布的Instagram专业账号")
            raise RuntimeError("当前账号没有可绑定的主页或频道")
        pending = protect(json.dumps({"token": token, "candidates": candidates}, ensure_ascii=False))
        with connect() as conn:
            conn.execute("UPDATE oauth_sessions SET status='selecting_account',result_encrypted=?,completed_at=?,error_code='',error_message='' WHERE state=?", (pending, now(), state))
        return session_status(state)
    except Exception as exc:
        error_code, message = _oauth_error(exc)
        if str(exc).startswith("当前账号"):
            message = str(exc)
        with connect() as conn:
            conn.execute("UPDATE oauth_sessions SET status='authorization_failed',error_code=?,error_message=?,completed_at=? WHERE state=?", (error_code, message, now(), state))
            conn.execute("UPDATE publish_accounts SET authorization_status='not_authorized',last_error=?,updated_at=? WHERE id=?", (message, now(), account["id"]))
        raise RuntimeError(message) from exc


def select_oauth_account(state: str, external_id: str) -> dict:
    with connect() as conn:
        session_row = conn.execute("SELECT * FROM oauth_sessions WHERE state=?", (state,)).fetchone()
        if not session_row:
            raise ValueError("授权会话不存在")
        session = dict(session_row)
        if session["status"] != "selecting_account" or not session.get("result_encrypted"):
            raise ValueError("当前授权会话不能选择账号")
        account_row = conn.execute("SELECT * FROM publish_accounts WHERE id=? AND status='active'", (session["account_id"],)).fetchone()
    pending = json.loads(unprotect(session["result_encrypted"]))
    candidate = next((item for item in pending.get("candidates") or [] if str(item.get("id")) == str(external_id)), None)
    if not candidate:
        raise ValueError("选择的平台账号不存在")
    account = dict(account_row)
    provider = normalize_provider(session["provider"])
    platform_code = PROVIDERS[provider]["platform_code"]
    with connect() as conn:
        duplicate = conn.execute(
            """SELECT a.id FROM publish_accounts a JOIN platforms p ON p.id=a.platform_id
               WHERE p.code=? AND a.channel_id=? AND a.status='active' AND a.id<>?""",
            (platform_code, str(candidate["id"]), account["id"]),
        ).fetchone()
        if duplicate:
            raise ValueError("该账号已经添加，无需重复授权")
    token = pending.get("token") or {}
    access_token = str(candidate.get("page_access_token") or token.get("access_token") or "")
    expires_at = ""
    if token.get("expires_in"):
        expires_at = (datetime.now() + timedelta(seconds=int(token["expires_in"]))).strftime("%Y-%m-%d %H:%M:%S")
    stamp = now()
    with connect() as conn:
        conn.execute(
            """INSERT INTO oauth_tokens(account_id,provider,access_token_encrypted,refresh_token_encrypted,
               token_type,scope,expires_at,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(account_id) DO UPDATE SET provider=excluded.provider,
               access_token_encrypted=excluded.access_token_encrypted,
               refresh_token_encrypted=CASE WHEN excluded.refresh_token_encrypted='' THEN oauth_tokens.refresh_token_encrypted ELSE excluded.refresh_token_encrypted END,
               token_type=excluded.token_type,scope=excluded.scope,expires_at=excluded.expires_at,status='authorized',updated_at=excluded.updated_at""",
            (account["id"], provider, protect(access_token), protect(str(token.get("refresh_token") or "")),
             str(token.get("token_type") or "Bearer"), str(token.get("scope") or ""), expires_at, "authorized", stamp, stamp),
        )
        title = str(candidate.get("name") or candidate.get("username") or account["account_name"])
        conn.execute(
            """UPDATE publish_accounts SET authorization_status='authorized',channel_id=?,channel_title=?,
               last_checked_at=?,last_error='',updated_at=? WHERE id=?""",
            (str(candidate["id"]), title, stamp, stamp, account["id"]),
        )
        conn.execute("UPDATE oauth_sessions SET status='authorized',result_encrypted='',completed_at=? WHERE state=?", (stamp, state))
    refresh_available = bool(token.get("refresh_token"))
    message = "账号授权已完成"
    if provider == "x" and not refresh_available:
        message = "账号授权已完成；当前未获得长期授权，Token到期后需要重新登录"
    return {
        "status": "authorized", "provider": provider, "platform": PROVIDERS[provider]["name"],
        "account_id": account["id"], "channel_id_masked": _mask_id(str(candidate["id"])),
        "channel_title": title, "message": message, "refresh_available": refresh_available,
        "publication_capability": (_platform_for_provider(provider) or {}).get("capability_status", "untested"),
    }


def _mask_id(value: str) -> str:
    value = str(value or "")
    if len(value) <= 6:
        return "***" if value else ""
    return value[:3] + "***" + value[-3:]


def authorization_status(account_id: int) -> dict:
    with connect() as conn:
        token = conn.execute("SELECT expires_at,status,updated_at FROM oauth_tokens WHERE account_id=?", (account_id,)).fetchone()
    if not token:
        return {"status": "not_authorized", "expires_at": ""}
    expired = bool(token["expires_at"] and token["expires_at"] <= now())
    return {"status": "token_expired" if expired else token["status"], "expires_at": token["expires_at"]}
