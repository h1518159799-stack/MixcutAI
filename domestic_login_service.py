"""Long-lived browser login sessions for Xiaohongshu and WeChat Channels.

This module deliberately does not share the old cookie-only browser flow.  A
login is accepted only when both platform cookies and authenticated page
evidence agree.  The Playwright context is owned by a background session, not
by the HTTP request that created it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from browser_profile_service import ensure_profile, find_chromium
from runtime_paths import BROWSER_PROFILE_DIR, LOG_DIR
from subprocess_utils import background_process_kwargs


SUPPORTED_PLATFORMS = {"xiaohongshu", "wechat_channels"}
LOGIN_SESSION_TIMEOUT_SECONDS = 15 * 60
LOGIN_VERIFY_TIMEOUT_SECONDS = 60
LOGIN_POLL_SECONDS = 2.0
LOGIN_SUCCESS_GRACE_SECONDS = 2.5

LOGIN_URLS = {
    "xiaohongshu": "https://creator.xiaohongshu.com/",
    "wechat_channels": "https://channels.weixin.qq.com/",
}

# Login entry pages and read-only session verification pages are deliberately
# different for WeChat Channels.  Opening the site root after a browser restart
# can remain on the landing/login shell even when the persistent cookies are
# valid.  The authenticated creator route performs the platform's own redirect
# check and gives us reliable page evidence without changing the profile.
PROBE_URLS = {
    "xiaohongshu": "https://creator.xiaohongshu.com/new/home",
    "wechat_channels": "https://channels.weixin.qq.com/platform",
}

PLATFORM_NAMES = {"xiaohongshu": "小红书", "wechat_channels": "视频号"}

ERROR_MESSAGES = {
    "BROWSER_LAUNCH_FAILED": "无法打开登录浏览器",
    "PROFILE_LOCKED": "该账号登录环境正在使用，请在已打开的窗口中完成登录",
    "LOGIN_TIMEOUT": "等待登录超时，请重新登录",
    "LOGIN_VERIFY_TIMEOUT": "登录状态确认超时，请重新登录",
    "LOGIN_PAGE_CHANGED": "平台登录页面发生变化，请更新软件后重试",
    "LOGIN_BROWSER_CLOSED": "登录窗口已关闭，尚未完成登录",
    "LOGIN_VERIFY_FAILED": "已完成登录操作，但软件暂时无法确认登录状态",
    "NETWORK_ERROR": "当前无法访问平台登录页面，请检查网络后重试",
}

_XHS_AUTH_COOKIES = {
    "access-token-creator.xiaohongshu.com",
    "customer-sso-sid",
    "galaxy.creator.beaker.session.id",
    "galaxy_creator_session_id",
    "x-user-id-creator.xiaohongshu.com",
}
_XHS_AUTH_TEXT = ("发布笔记", "笔记管理", "数据看板", "创作服务平台")
_WECHAT_AUTH_COOKIES = {"sessionid", "wxuin"}
_WECHAT_AUTH_TEXT = ("发表动态", "内容管理", "视频管理", "直播管理", "数据中心")


@dataclass
class BrowserLoginSession:
    session_id: str
    account_id: int
    platform: str
    profile_path: str
    browser_path: str
    browser_pid: int = 0
    context_status: str = "starting"
    page_url: str = ""
    page_title: str = ""
    started_at: float = field(default_factory=time.time)
    last_heartbeat_at: float = field(default_factory=time.time)
    login_status: str = "starting"
    error_code: str = ""
    error_message: str = "正在打开登录浏览器"
    screenshot_path: str = ""
    log_path: str = ""
    initial_login_status: str = "not_logged_in"
    close_reason: str = ""
    verification_started_at: float = 0.0
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "account_id": self.account_id,
            "platform": self.platform,
            "platform_name": PLATFORM_NAMES.get(self.platform, self.platform),
            "profile_path": self.profile_path,
            "browser_pid": self.browser_pid,
            "context_status": self.context_status,
            "page_url": self.page_url,
            "page_title": self.page_title,
            "started_at": self.started_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "login_status": self.login_status,
            # Existing UI/API convention uses `status`.
            "status": self.login_status,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "message": self.error_message,
            "screenshot_path": self.screenshot_path,
            "log_path": self.log_path,
            "elapsed_seconds": round(max(0.0, time.time() - self.started_at), 1),
        }


_LOCK = threading.RLock()
_SESSIONS: dict[int, BrowserLoginSession] = {}
_LAST_RESULTS: dict[int, dict[str, Any]] = {}


def _safe_url(value: str) -> str:
    try:
        parts = urlsplit(str(value or ""))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        return ""


def _diag_dir(platform: str, account_id: int) -> Path:
    path = LOG_DIR / "browser_login" / platform / str(account_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _append_log(session: BrowserLoginSession, event: str, detail: str = "") -> None:
    safe = str(detail or "").replace("\r", " ").replace("\n", " ")
    safe = re.sub(
        r"(?i)(access_token|refresh_token|authorization|cookie|password|sessionid|wxuin)\s*[:=]\s*[^\s&]+",
        r"\1=***",
        safe,
    )
    safe = re.sub(r"https?://[^\s]+", lambda match: _safe_url(match.group(0)), safe)[:1000]
    record = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "platform": session.platform,
        "account_id": session.account_id,
        "session_id": session.session_id,
        "event": event,
        "detail": safe,
        "browser_pid": session.browser_pid,
        "url": _safe_url(session.page_url),
        "title": session.page_title[:160],
    }
    try:
        with Path(session.log_path).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _set_state(
    session: BrowserLoginSession,
    status: str,
    message: str,
    error_code: str = "",
    *,
    context_status: str | None = None,
) -> None:
    changed = status != session.login_status or error_code != session.error_code or message != session.error_message
    session.login_status = status
    session.error_code = error_code
    session.error_message = message
    session.last_heartbeat_at = time.time()
    if context_status is not None:
        session.context_status = context_status
    if changed:
        _append_log(session, status, error_code or message)


def _account_columns() -> set[str]:
    from db import connect

    with connect() as conn:
        return {str(row[1]) for row in conn.execute("PRAGMA table_info(publish_accounts)").fetchall()}


def _mark_account(
    account_id: int,
    status: str,
    message: str = "",
    *,
    profile_path: str = "",
    success: bool = False,
) -> None:
    """Commit login state without touching platform publication capability."""
    from db import connect, now

    stamp = now()
    columns = _account_columns()
    assignments = ["login_status=?", "last_checked_at=?", "last_error=?", "updated_at=?"]
    values: list[Any] = [status, stamp, message[:500], stamp]
    if profile_path:
        assignments.append("browser_profile_path=?")
        values.append(profile_path)
    if success and "last_login_at" in columns:
        assignments.append("last_login_at=?")
        values.append(stamp)
    values.append(account_id)
    with connect() as conn:
        conn.execute(f"UPDATE publish_accounts SET {','.join(assignments)} WHERE id=?", tuple(values))


def _profile_locked_error(exc: Exception) -> bool:
    lowered = str(exc).lower()
    return any(word in lowered for word in ("profile", "singletonlock", "processsingleton", "user data directory")) and any(
        word in lowered for word in ("use", "lock", "already", "another")
    )


def _network_error(exc: Exception | str) -> bool:
    lowered = str(exc).lower()
    return any(word in lowered for word in ("net::", "timeout", "connection", "dns", "internet_disconnected", "name_not_resolved"))


def domestic_profile_path(platform: str, account_id: int) -> Path:
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError("当前平台不使用国内平台登录会话")
    return BROWSER_PROFILE_DIR / platform / str(int(account_id))


def ensure_domestic_profile(account_id: int, platform: str, current_path: str | Path) -> Path:
    """Move the legacy account-name profile to the stable account-id path."""
    target = domestic_profile_path(platform, account_id).resolve()
    root = BROWSER_PROFILE_DIR.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    source = Path(current_path).expanduser().resolve() if str(current_path or "").strip() else target
    if source != target and source.exists() and root in source.parents:
        try:
            if not target.exists():
                shutil.move(str(source), str(target))
            elif not any(target.iterdir()) and any(source.iterdir()):
                target.rmdir()
                shutil.move(str(source), str(target))
        except OSError as exc:
            raise RuntimeError(ERROR_MESSAGES["PROFILE_LOCKED"]) from exc
    return ensure_profile(target)


def _find_browser_pid(profile_path: str) -> int:
    """Best-effort diagnostic PID lookup; failure never affects login."""
    if not profile_path or not hasattr(subprocess, "CREATE_NO_WINDOW"):
        return 0
    escaped = profile_path.replace("'", "''")
    command = (
        "$p='" + escaped + "';"
        "Get-CimInstance Win32_Process | Where-Object {"
        "$_.Name -match '^(chrome|msedge|chromium)\\.exe$' -and $_.CommandLine -like ('*'+$p+'*')} | "
        "Select-Object -First 1 -ExpandProperty ProcessId"
    )
    try:
        process_command = ["powershell", "-NoProfile", "-Command", command]
        result = subprocess.run(
            process_command,
            capture_output=True,
            text=True,
            timeout=4,
            **background_process_kwargs(process_command, "browser_pid_detection"),
        )
        return int((result.stdout or "0").strip().splitlines()[0])
    except Exception:
        return 0


def _page_text(page: Any) -> str:
    try:
        return page.locator("body").inner_text(timeout=2500)[:50000]
    except Exception:
        return ""


def _active_page(context: Any, platform: str) -> Any | None:
    candidates = [page for page in context.pages if not page.is_closed()]
    if not candidates:
        return None
    domain = "xiaohongshu.com" if platform == "xiaohongshu" else "channels.weixin.qq.com"
    matching = [page for page in candidates if domain in str(page.url or "").lower()]
    return (matching or candidates)[-1]


def _cookie_names(cookies: list[dict[str, Any]], platform: str) -> set[str]:
    allowed_domain = "xiaohongshu.com" if platform == "xiaohongshu" else "channels.weixin.qq.com"
    return {
        str(cookie.get("name") or "").lower()
        for cookie in cookies
        if str(cookie.get("domain") or "").lower().lstrip(".").endswith(allowed_domain)
    }


def authenticated_platform_page(
    platform: str,
    cookies: list[dict[str, Any]],
    url: str,
    title: str = "",
    visible_text: str = "",
) -> bool:
    """Require cookie evidence plus an authenticated platform page."""
    lowered_url = str(url or "").lower()
    text = " ".join(str(visible_text or "").split())
    names = _cookie_names(cookies, platform)
    if platform == "xiaohongshu":
        if "/login" in lowered_url or "扫码登录" in text or "登录小红书" in text:
            return False
        cookie_evidence = len(names & _XHS_AUTH_COOKIES) >= 2
        route_evidence = any(part in lowered_url for part in ("/new/home", "/publish", "/new/manage", "/data"))
        text_evidence = sum(1 for marker in _XHS_AUTH_TEXT if marker in text) >= 2
        return cookie_evidence and (route_evidence or text_evidence)
    if platform == "wechat_channels":
        if "/login" in lowered_url or "登录视频号助手" in text:
            return False
        cookie_evidence = bool(names & _WECHAT_AUTH_COOKIES)
        route_evidence = any(part in lowered_url for part in ("/platform", "/post/", "/live/", "/data/"))
        text_evidence = any(marker in text for marker in _WECHAT_AUTH_TEXT)
        return cookie_evidence or route_evidence or text_evidence
    return False


def _waiting_message(platform: str, url: str, text: str) -> tuple[str, str]:
    normalized = " ".join(str(text or "").split())
    if "加载失败" in normalized or "点击重试" in normalized:
        return "NETWORK_ERROR", ERROR_MESSAGES["NETWORK_ERROR"]
    if any(marker in normalized for marker in ("验证码", "安全验证", "手机确认", "请完成验证")):
        return "", f"请在{PLATFORM_NAMES[platform]}页面完成验证码或手机确认"
    if platform == "wechat_channels":
        return "", "正在等待视频号扫码登录和手机确认"
    return "", "正在等待用户完成小红书登录"


def _success_screenshot(page: Any, session: BrowserLoginSession) -> None:
    try:
        target = _diag_dir(session.platform, session.account_id) / f"{time.strftime('%Y%m%d-%H%M%S')}-logged-in.png"
        page.screenshot(path=str(target), full_page=False)
        session.screenshot_path = str(target)
    except Exception:
        pass


def _probe_with_playwright(playwright: Any, platform: str, profile: Path, browser: Path) -> dict[str, Any]:
    context = None
    try:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            executable_path=str(browser),
            headless=True,
            args=["--no-first-run", "--no-default-browser-check"],
        )
        page = context.pages[-1] if context.pages else context.new_page()
        try:
            page.goto(PROBE_URLS[platform], wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            if _network_error(exc):
                return {"status": "login_required", "error_code": "NETWORK_ERROR", "message": ERROR_MESSAGES["NETWORK_ERROR"]}
            raise
        page.wait_for_timeout(1800)
        page = _active_page(context, platform) or page
        url = page.url
        title = page.title()
        text = _page_text(page)
        if authenticated_platform_page(platform, context.cookies(), url, title, text):
            return {
                "status": "logged_in", "error_code": "", "message": f"{PLATFORM_NAMES[platform]}登录状态有效",
                "page_url": _safe_url(url), "page_title": title,
            }
        code, message = _waiting_message(platform, url, text)
        return {
            "status": "login_required", "error_code": code or "LOGIN_VERIFY_FAILED", "message": message,
            "page_url": _safe_url(url), "page_title": title,
        }
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass


def _run_login(session: BrowserLoginSession) -> None:
    context = None
    authenticated = False
    manual_browser_close = False
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            try:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=session.profile_path,
                    executable_path=session.browser_path,
                    headless=False,
                    args=["--no-first-run", "--no-default-browser-check", "--start-maximized"],
                    viewport=None,
                )
            except Exception as exc:
                code = "PROFILE_LOCKED" if _profile_locked_error(exc) else "BROWSER_LAUNCH_FAILED"
                _set_state(session, "failed", ERROR_MESSAGES[code], code, context_status="closed")
                _mark_account(session.account_id, session.initial_login_status, ERROR_MESSAGES[code], profile_path=session.profile_path)
                return
            session.context_status = "browser_open"
            session.browser_pid = _find_browser_pid(session.profile_path)
            _set_state(session, "browser_open", f"{PLATFORM_NAMES[session.platform]}登录窗口已打开", context_status="browser_open")
            page = _active_page(context, session.platform) or context.new_page()
            try:
                page.goto(LOGIN_URLS[session.platform], wait_until="domcontentloaded", timeout=60000)
            except Exception as exc:
                if _network_error(exc):
                    _set_state(session, "waiting_login", ERROR_MESSAGES["NETWORK_ERROR"], "NETWORK_ERROR")
                else:
                    _append_log(session, "navigation_warning", type(exc).__name__)
            last_page_id = ""
            confirmation_count = 0
            while not session.stop_event.wait(LOGIN_POLL_SECONDS):
                if time.time() - session.started_at >= LOGIN_SESSION_TIMEOUT_SECONDS:
                    session.close_reason = "timeout"
                    _set_state(session, "expired", ERROR_MESSAGES["LOGIN_TIMEOUT"], "LOGIN_TIMEOUT", context_status="closing")
                    _mark_account(session.account_id, session.initial_login_status, ERROR_MESSAGES["LOGIN_TIMEOUT"], profile_path=session.profile_path)
                    break
                try:
                    page = _active_page(context, session.platform)
                except Exception as exc:
                    if "closed" in str(exc).lower() or "target" in str(exc).lower():
                        manual_browser_close = True
                        session.close_reason = "browser_closed"
                        break
                    raise
                if page is None:
                    manual_browser_close = True
                    session.close_reason = "browser_closed"
                    break
                page_id = f"{id(page)}:{_safe_url(page.url)}"
                if page_id != last_page_id:
                    last_page_id = page_id
                    _append_log(session, "active_page_changed", _safe_url(page.url))
                session.page_url = _safe_url(page.url)
                try:
                    session.page_title = page.title()
                except Exception:
                    session.page_title = ""
                session.last_heartbeat_at = time.time()
                text = _page_text(page)
                try:
                    cookies = context.cookies()
                except Exception as exc:
                    if "closed" in str(exc).lower() or "target" in str(exc).lower():
                        manual_browser_close = True
                        session.close_reason = "browser_closed"
                        break
                    raise
                login_detected = authenticated_platform_page(
                    session.platform, cookies, page.url, session.page_title, text
                )
                _append_log(
                    session,
                    "login_detection",
                    json.dumps({
                        "cookie_count": len(_cookie_names(cookies, session.platform)),
                        "authenticated": login_detected,
                        "url": _safe_url(page.url),
                        "title": session.page_title[:160],
                    }, ensure_ascii=False),
                )
                if login_detected:
                    if not session.verification_started_at:
                        session.verification_started_at = time.time()
                    confirmation_count += 1
                    _set_state(session, "verifying", "正在确认登录状态", context_status="browser_open")
                    if time.time() - session.verification_started_at >= LOGIN_VERIFY_TIMEOUT_SECONDS:
                        session.close_reason = "verification_timeout"
                        _set_state(
                            session, "expired", "登录状态确认超时，请重新登录",
                            "LOGIN_VERIFY_TIMEOUT", context_status="closing",
                        )
                        _mark_account(
                            session.account_id, session.initial_login_status,
                            "登录状态确认超时，请重新登录", profile_path=session.profile_path,
                        )
                        break
                    if confirmation_count < 2:
                        continue
                    # Commit first.  Publication capability is intentionally untouched.
                    _mark_account(session.account_id, "logged_in", "", profile_path=session.profile_path, success=True)
                    authenticated = True
                    _success_screenshot(page, session)
                    _set_state(session, "logged_in", f"{PLATFORM_NAMES[session.platform]}登录成功", context_status="saving_profile")
                    # Leave enough time for the 2-second frontend poll to observe success.
                    session.stop_event.wait(LOGIN_SUCCESS_GRACE_SECONDS)
                    session.close_reason = "login_success"
                    break
                if session.verification_started_at:
                    if time.time() - session.verification_started_at >= LOGIN_VERIFY_TIMEOUT_SECONDS:
                        session.close_reason = "verification_timeout"
                        _set_state(
                            session, "expired", "登录状态确认超时，请重新登录",
                            "LOGIN_VERIFY_TIMEOUT", context_status="closing",
                        )
                        _mark_account(
                            session.account_id, session.initial_login_status,
                            "登录状态确认超时，请重新登录", profile_path=session.profile_path,
                        )
                        break
                else:
                    confirmation_count = 0
                code, message = _waiting_message(session.platform, page.url, text)
                _set_state(session, "waiting_login", message, code, context_status="browser_open")
            if session.stop_event.is_set() and not authenticated:
                session.close_reason = "user_cancelled"
                restored = "logged_in" if session.initial_login_status == "logged_in" else "login_required"
                _mark_account(session.account_id, restored, "", profile_path=session.profile_path)
                _set_state(session, "cancelled", "已取消登录", context_status="closing")
            try:
                context.close()
            except Exception:
                pass
            context = None
            if manual_browser_close and not session.stop_event.is_set() and not authenticated:
                # The user may have completed login immediately before closing the window.
                time.sleep(0.6)
                try:
                    probe = _probe_with_playwright(playwright, session.platform, Path(session.profile_path), Path(session.browser_path))
                except Exception:
                    probe = {"status": "login_required"}
                if probe.get("status") == "logged_in":
                    _mark_account(session.account_id, "logged_in", "", profile_path=session.profile_path, success=True)
                    _set_state(session, "logged_in", f"{PLATFORM_NAMES[session.platform]}登录成功", context_status="closed")
                else:
                    restored = "logged_in" if session.initial_login_status == "logged_in" else "login_required"
                    _mark_account(session.account_id, restored, ERROR_MESSAGES["LOGIN_BROWSER_CLOSED"], profile_path=session.profile_path)
                    _set_state(session, "cancelled", ERROR_MESSAGES["LOGIN_BROWSER_CLOSED"], "LOGIN_BROWSER_CLOSED", context_status="closed")
    except Exception as exc:
        if not session.stop_event.is_set() and not authenticated:
            if _profile_locked_error(exc):
                code = "PROFILE_LOCKED"
            elif _network_error(exc):
                code = "NETWORK_ERROR"
            elif "closed" in str(exc).lower() or "target page" in str(exc).lower():
                code = "LOGIN_BROWSER_CLOSED"
            else:
                code = "LOGIN_PAGE_CHANGED"
            _append_log(session, "engine_error", type(exc).__name__)
            restored = "logged_in" if session.initial_login_status == "logged_in" else "login_required"
            _mark_account(session.account_id, restored, ERROR_MESSAGES[code], profile_path=session.profile_path)
            _set_state(session, "failed" if code != "LOGIN_BROWSER_CLOSED" else "cancelled", ERROR_MESSAGES[code], code, context_status="closed")
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        session.context_status = "closed"
        session.last_heartbeat_at = time.time()
        _append_log(session, "session_closed", session.close_reason or session.login_status)
        with _LOCK:
            _LAST_RESULTS[session.account_id] = session.public()
            if _SESSIONS.get(session.account_id) is session:
                _SESSIONS.pop(session.account_id, None)


def launch_domestic_login(
    account_id: int,
    platform: str,
    current_profile_path: str,
    *,
    initial_login_status: str = "not_logged_in",
) -> dict[str, Any]:
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError("当前平台不使用国内平台登录会话")
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("Playwright未安装，请重新安装发布版") from exc
    with _LOCK:
        running = _SESSIONS.get(account_id)
        if running and running.thread and running.thread.is_alive():
            return {**running.public(), "already_open": True, "message": "该账号的登录窗口已经打开"}
    profile = ensure_domestic_profile(account_id, platform, current_profile_path)
    browser = find_chromium()
    _mark_account(account_id, "login_pending", "", profile_path=str(profile))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    session = BrowserLoginSession(
        session_id=uuid.uuid4().hex,
        account_id=account_id,
        platform=platform,
        profile_path=str(profile),
        browser_path=str(browser),
        initial_login_status=str(initial_login_status or "not_logged_in"),
        log_path=str(_diag_dir(platform, account_id) / f"login-{stamp}.jsonl"),
    )
    session.thread = threading.Thread(
        target=_run_login,
        args=(session,),
        name=f"browser-login-{platform}-{account_id}",
        daemon=True,
    )
    with _LOCK:
        _SESSIONS[account_id] = session
        _LAST_RESULTS.pop(account_id, None)
    session.thread.start()
    return session.public()


def domestic_login_status(account_id: int, account: dict[str, Any] | None = None) -> dict[str, Any]:
    with _LOCK:
        session = _SESSIONS.get(account_id)
        if session:
            return session.public()
        previous = _LAST_RESULTS.get(account_id)
    if previous:
        return dict(previous)
    status = str((account or {}).get("login_status") or "not_logged_in")
    platform = str((account or {}).get("platform_code") or "")
    return {
        "account_id": account_id,
        "platform": platform,
        "platform_name": PLATFORM_NAMES.get(platform, "平台"),
        "status": status,
        "login_status": status,
        "message": f"{PLATFORM_NAMES.get(platform, '平台')}账号已登录" if status == "logged_in" else "账号尚未登录",
        "error_code": "" if status == "logged_in" else "LOGIN_VERIFY_FAILED",
        "profile_path": str((account or {}).get("browser_profile_path") or ""),
        "context_status": "closed",
    }


def cancel_domestic_login(account_id: int) -> dict[str, Any]:
    with _LOCK:
        session = _SESSIONS.get(account_id)
        if session and session.thread and session.thread.is_alive():
            session.stop_event.set()
            return {**session.public(), "message": "正在关闭登录窗口"}
    return {"account_id": account_id, "status": "cancelled", "login_status": "cancelled", "message": "当前没有正在进行的登录"}


def probe_domestic_login(account_id: int, platform: str, current_profile_path: str) -> dict[str, Any]:
    """Read-only login verification. It never uploads, publishes or clears state."""
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError("当前平台不使用国内平台登录检测")
    with _LOCK:
        running = _SESSIONS.get(account_id)
        if running and running.thread and running.thread.is_alive():
            return running.public()
    profile = ensure_domestic_profile(account_id, platform, current_profile_path)
    browser = find_chromium()
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            result = _probe_with_playwright(playwright, platform, profile, browser)
    except Exception as exc:
        if _profile_locked_error(exc):
            result = {"status": "needs_manual_action", "error_code": "PROFILE_LOCKED", "message": ERROR_MESSAGES["PROFILE_LOCKED"]}
        elif _network_error(exc):
            result = {"status": "login_required", "error_code": "NETWORK_ERROR", "message": ERROR_MESSAGES["NETWORK_ERROR"]}
        else:
            result = {"status": "check_failed", "error_code": "LOGIN_PAGE_CHANGED", "message": ERROR_MESSAGES["LOGIN_PAGE_CHANGED"]}
    result.update({"account_id": account_id, "platform": platform, "profile_path": str(profile)})
    saved_status = "logged_in" if result["status"] == "logged_in" else str(result["status"])
    _mark_account(
        account_id,
        saved_status,
        "" if saved_status == "logged_in" else str(result.get("message") or ""),
        profile_path=str(profile),
        success=saved_status == "logged_in",
    )
    return result


def reset_domestic_login_state_for_tests() -> None:
    with _LOCK:
        for session in _SESSIONS.values():
            session.stop_event.set()
        _SESSIONS.clear()
        _LAST_RESULTS.clear()
