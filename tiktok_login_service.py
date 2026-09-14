"""TikTok browser login workflow using one persistent Chromium profile per account.

This module intentionally does not automate credentials, CAPTCHA, security
verification, uploading, or publishing.  It only opens TikTok's official login
pages, watches the visible state, and verifies a completed browser session.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from browser_auth_service import _is_authenticated
from browser_profile_service import ensure_profile, find_system_browser

# Compatibility name for older integrations/tests. TikTok login never uses
# the bundled Chromium through this alias; it resolves only installed Chrome/Edge.
find_chromium = find_system_browser
from runtime_paths import LOG_DIR
from subprocess_utils import background_process_kwargs


TIKTOK_LOGIN_URLS = {
    "qr": "https://www.tiktok.com/login/qrcode",
    "account": "https://www.tiktok.com/login/phone-or-email/email",
}
TIKTOK_CHECK_URL = "https://www.tiktok.com/upload?lang=zh-Hans"

_QR_FAILURE_TEXT = (
    "无法登录，请尝试其它登录方式",
    "无法登录，请尝试其他登录方式",
    "couldn't log in. try another way",
    "unable to log in. try another",
)
_VERIFICATION_TEXT = (
    "请完成验证",
    "安全验证",
    "输入验证码",
    "二次验证",
    "verify it's you",
    "security verification",
    "enter the code",
)
_AUTH_SELECTORS = (
    '[data-e2e="profile-icon"]',
    '[data-e2e="nav-profile"]',
    '[data-e2e="upload-icon"]',
    'a[href*="/upload"]',
    'a[href^="/@"]',
)


@dataclass
class TikTokLoginSession:
    account_id: int
    profile_path: str
    browser_path: str
    mode: str
    requested_mode: str
    status: str = "opening_browser"
    error_code: str = ""
    message: str = "正在打开TikTok官方登录页面"
    current_url: str = ""
    page_title: str = ""
    screenshot_path: str = ""
    log_path: str = ""
    browser_version: str = ""
    username: str = ""
    display_name: str = ""
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "mode": self.mode,
            "status": self.status,
            "error_code": self.error_code,
            "message": self.message,
            "current_url": self.current_url,
            "page_title": self.page_title,
            "profile_path": self.profile_path,
            "browser": self.browser_path,
            "browser_version": self.browser_version,
            "screenshot_path": self.screenshot_path,
            "log_path": self.log_path,
            "username": self.username,
            "display_name": self.display_name,
            "oauth_configured": False,
            "oauth_message": "TikTok官方API尚未配置",
            "elapsed_seconds": round(max(0.0, time.time() - self.started_at), 1),
        }


_LOCK = threading.RLock()
_SESSIONS: dict[int, TikTokLoginSession] = {}
_LAST_RESULTS: dict[int, dict[str, Any]] = {}


def normalize_login_mode(value: str) -> str:
    return "account" if str(value or "").strip().lower() == "account" else "qr"


def login_url_for_mode(mode: str) -> str:
    return TIKTOK_LOGIN_URLS[normalize_login_mode(mode)]


def classify_tiktok_page(url: str, visible_text: str) -> tuple[str, str, str] | None:
    """Classify only explicit states; absence of a match means keep waiting."""
    normalized = " ".join(str(visible_text or "").lower().split())
    lowered_url = str(url or "").lower()
    if any(text in normalized for text in _QR_FAILURE_TEXT):
        return (
            "qr_unavailable",
            "TIKTOK_QR_LOGIN_UNAVAILABLE",
            "二维码登录未成功，请尝试账号登录。",
        )
    if any(part in lowered_url for part in ("/captcha", "/challenge", "/verify")) or any(
        text in normalized for text in _VERIFICATION_TEXT
    ):
        return (
            "verification_required",
            "TIKTOK_VERIFICATION_REQUIRED",
            "请在TikTok页面完成安全验证。",
        )
    return None


def authenticated_page(cookies: list[dict[str, Any]], url: str, indicator_count: int) -> bool:
    """Require a real auth cookie and authenticated page evidence."""
    authenticated_cookie = _is_authenticated(cookies, ["tiktok.com"], ["sessionid", "sid_tt"])
    lowered = str(url or "").lower()
    if not authenticated_cookie:
        return False
    if any(part in lowered for part in ("/login", "/captcha", "/challenge", "/verify")):
        return False
    account_route = any(part in lowered for part in ("/upload", "/studio", "/creator", "/@"))
    return account_route or int(indicator_count or 0) > 0


def _browser_version(browser: Path) -> str:
    for manifest in browser.parent.glob("*.manifest"):
        if re.fullmatch(r"\d+(?:\.\d+){2,3}", manifest.stem):
            return manifest.stem
    try:
        import ctypes
        from ctypes import wintypes

        class VSFixedFileInfo(ctypes.Structure):
            _fields_ = [
                ("dwSignature", wintypes.DWORD), ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD), ("dwFileVersionLS", wintypes.DWORD),
                ("dwProductVersionMS", wintypes.DWORD), ("dwProductVersionLS", wintypes.DWORD),
                ("dwFileFlagsMask", wintypes.DWORD), ("dwFileFlags", wintypes.DWORD),
                ("dwFileOS", wintypes.DWORD), ("dwFileType", wintypes.DWORD),
                ("dwFileSubtype", wintypes.DWORD), ("dwFileDateMS", wintypes.DWORD),
                ("dwFileDateLS", wintypes.DWORD),
            ]

        size = ctypes.windll.version.GetFileVersionInfoSizeW(str(browser), None)
        if size:
            data = ctypes.create_string_buffer(size)
            ctypes.windll.version.GetFileVersionInfoW(str(browser), 0, size, data)
            pointer, length = ctypes.c_void_p(), ctypes.c_uint()
            ctypes.windll.version.VerQueryValueW(data, "\\", ctypes.byref(pointer), ctypes.byref(length))
            if pointer.value:
                info = ctypes.cast(pointer, ctypes.POINTER(VSFixedFileInfo)).contents
                return ".".join(str(value) for value in (
                    info.dwFileVersionMS >> 16,
                    info.dwFileVersionMS & 0xFFFF,
                    info.dwFileVersionLS >> 16,
                    info.dwFileVersionLS & 0xFFFF,
                ))
    except Exception:
        pass
    return "未知版本"


def _diag_dir(account_id: int) -> Path:
    target = LOG_DIR / "tiktok_login" / str(account_id)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _append_log(session: TikTokLoginSession, event: str, detail: str = "") -> None:
    target = Path(session.log_path)
    safe = str(detail or "").replace("\r", " ").replace("\n", " ")
    safe = re.sub(r"(?i)(access_token|refresh_token|authorization|cookie|password|sessionid|sid_tt)\s*[:=]\s*[^\s&]+", r"\1=***", safe)
    safe = re.sub(r'(?i)("?(?:user_unique_id|device_id|web_id|user_id|uid)"?\s*:\s*)"?[^",}\s]+"?', r'\1"***"', safe)
    safe = re.sub(r"https?://[^\s]+", lambda match: _safe_url(match.group(0)), safe)[:1200]
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "event": event,
            "detail": safe,
            "url": session.current_url,
        }, ensure_ascii=False) + "\n")


def _safe_url(value: str) -> str:
    try:
        parts = urlsplit(str(value or ""))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        return ""


def _screenshot(page: Any, session: TikTokLoginSession, label: str) -> None:
    try:
        path = _diag_dir(session.account_id) / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}.png"
        page.screenshot(path=str(path), full_page=False)
        session.screenshot_path = str(path)
    except Exception as exc:
        _append_log(session, "screenshot_failed", type(exc).__name__)


def _mark_account(account_id: int, status: str, message: str = "", *, username: str = "", display_name: str = "") -> None:
    from db import connect, now

    stamp = now()
    with connect() as conn:
        conn.execute(
            """UPDATE publish_accounts
               SET login_status=?,last_checked_at=?,last_error=?,channel_id=CASE WHEN ?<>'' THEN ? ELSE channel_id END,
                   channel_title=CASE WHEN ?<>'' THEN ? ELSE channel_title END,updated_at=? WHERE id=?""",
            (status, stamp, message[:500], username, username, display_name, display_name, stamp, account_id),
        )


def _set_state(session: TikTokLoginSession, status: str, code: str, message: str, page: Any | None = None) -> None:
    changed = status != session.status or code != session.error_code
    session.status, session.error_code, session.message = status, code, message
    session.updated_at = time.time()
    if changed:
        _append_log(session, status, code or message)
        if page is not None:
            _screenshot(page, session, status)
    db_status = {
        "opening_browser": "login_pending",
        "waiting_login": "login_pending",
        "qr_unavailable": "qr_unavailable",
        "verification_required": "verification_required",
        "network_error": "network_error",
        "page_changed": "page_changed",
        "cancelled": "login_required",
        "browser_closed": "login_required",
    }.get(status, status)
    _mark_account(session.account_id, db_status, "" if status == "logged_in" else message)


def _page_text(page: Any) -> str:
    try:
        return page.locator("body").inner_text(timeout=2500)[:50000]
    except Exception:
        return ""


def _indicator_count(page: Any) -> int:
    count = 0
    for selector in _AUTH_SELECTORS:
        try:
            if page.locator(selector).first.is_visible(timeout=350):
                count += 1
        except Exception:
            continue
    return count


def _identity(page: Any) -> tuple[str, str]:
    username = display_name = ""
    try:
        href = page.locator('a[href^="/@"]').first.get_attribute("href", timeout=800) or ""
        match = re.search(r"/@([^/?#]+)", href)
        username = match.group(1) if match else ""
    except Exception:
        pass
    for selector in ('[data-e2e="profile-name"]', '[data-e2e="user-title"]', 'a[href^="/@"]'):
        try:
            value = page.locator(selector).first.inner_text(timeout=500).strip()
            if value:
                display_name = value[:120]
                break
        except Exception:
            continue
    return username, display_name


def _run_login(session: TikTokLoginSession) -> None:
    authenticated = False
    context = None
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=session.profile_path,
                executable_path=session.browser_path,
                headless=False,
                args=["--no-first-run", "--no-default-browser-check", "--start-maximized"],
                viewport=None,
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.on("console", lambda msg: _append_log(session, "console", f"{msg.type}: {msg.text}"))
            page.on("requestfailed", lambda req: _append_log(session, "request_failed", f"{req.method} {req.url}"))
            current_mode = ""
            while not session.stop_event.is_set():
                if page.is_closed():
                    _set_state(session, "browser_closed", "TIKTOK_LOGIN_REQUIRED", "TikTok登录窗口已关闭，可重新打开继续登录。")
                    break
                if session.requested_mode != current_mode:
                    current_mode = session.requested_mode
                    session.mode = current_mode
                    page.goto(login_url_for_mode(current_mode), wait_until="domcontentloaded", timeout=60000)
                    session.current_url = page.url
                    try:
                        session.page_title = page.title()
                    except Exception:
                        session.page_title = ""
                    _set_state(session, "waiting_login", "", "正在等待TikTok登录", page)
                    _screenshot(page, session, f"{current_mode}-login-page")
                page.wait_for_timeout(1000)
                session.current_url = page.url
                try:
                    session.page_title = page.title()
                except Exception:
                    session.page_title = ""
                text = _page_text(page)
                state = classify_tiktok_page(session.current_url, text)
                if state:
                    _set_state(session, *state, page)
                    continue
                indicators = _indicator_count(page)
                if authenticated_page(context.cookies(), session.current_url, indicators):
                    authenticated = True
                    session.username, session.display_name = _identity(page)
                    _set_state(session, "logged_in", "", "TikTok账号登录成功", page)
                    _mark_account(
                        session.account_id, "logged_in", "",
                        username=session.username, display_name=session.display_name,
                    )
                    context.close()
                    break
                if session.status in {"qr_unavailable", "verification_required"}:
                    _set_state(session, "waiting_login", "", "正在等待TikTok登录")
    except Exception as exc:
        message = str(exc)
        manual_close = "closed" in message.lower() or "target page" in message.lower()
        if not authenticated and not session.stop_event.is_set():
            if manual_close:
                _set_state(session, "browser_closed", "TIKTOK_LOGIN_REQUIRED", "TikTok登录窗口已关闭，可重新打开继续登录。")
            elif any(word in message.lower() for word in ("net::", "timeout", "connection", "dns")):
                _set_state(session, "network_error", "TIKTOK_NETWORK_ERROR", "当前无法连接TikTok，请检查海外网络环境。")
            else:
                _append_log(session, "engine_error", type(exc).__name__)
                _set_state(session, "page_changed", "TIKTOK_PAGE_CHANGED", "TikTok登录页面发生变化，请更新软件后重试。")
    finally:
        try:
            if context is not None:
                context.close()
        except Exception:
            pass
        if session.stop_event.is_set() and session.status != "logged_in":
            _set_state(session, "cancelled", "TIKTOK_LOGIN_REQUIRED", "已取消TikTok登录。")
        with _LOCK:
            _LAST_RESULTS[session.account_id] = session.public()
            if _SESSIONS.get(session.account_id) is session:
                _SESSIONS.pop(session.account_id, None)


def launch_tiktok_login(account_id: int, profile_path: str, mode: str = "qr") -> dict[str, Any]:
    profile = ensure_profile(profile_path)
    browser = find_system_browser()
    selected_mode = normalize_login_mode(mode)
    with _LOCK:
        existing = _SESSIONS.get(account_id)
        if existing and existing.thread and existing.thread.is_alive():
            existing.requested_mode = selected_mode
            return {"account_id": account_id, "status": "browser_open", "message": "TikTok登录浏览器已打开，请完成登录后点击检测。", "profile_path": str(profile), "browser": str(browser), "browser_version": _browser_version(browser)}
    import subprocess
    command = [str(browser), f"--user-data-dir={profile}", "--profile-directory=Default", "--no-first-run", "--no-default-browser-check", "--new-window", login_url_for_mode(selected_mode)]
    process = subprocess.Popen(command, cwd=str(browser.parent), close_fds=True,
                               **background_process_kwargs(command, "tiktok_login_browser"))
    return {"account_id": account_id, "status": "browser_open", "message": "请在系统Chrome中完成TikTok登录，完成后返回软件点击检测登录状态。", "profile_path": str(profile), "browser": str(browser), "browser_version": _browser_version(browser), "pid": process.pid, "login_mode": selected_mode}


def tiktok_login_status(account_id: int, account: dict[str, Any] | None = None) -> dict[str, Any]:
    with _LOCK:
        session = _SESSIONS.get(account_id)
        if session:
            return session.public()
        previous = _LAST_RESULTS.get(account_id)
    if previous:
        return dict(previous)
    status = str((account or {}).get("login_status") or "not_logged_in")
    return {
        "account_id": account_id,
        "status": status,
        "message": {
            "logged_in": "TikTok账号已登录",
            "verification_required": "请在TikTok页面完成安全验证。",
            "qr_unavailable": "二维码登录未成功，请尝试账号登录。",
        }.get(status, "TikTok账号尚未登录"),
        "error_code": "" if status == "logged_in" else "TIKTOK_LOGIN_REQUIRED",
        "profile_path": str((account or {}).get("browser_profile_path") or ""),
        "oauth_configured": False,
        "oauth_message": "TikTok官方API尚未配置",
    }


def cancel_tiktok_login(account_id: int) -> dict[str, Any]:
    with _LOCK:
        session = _SESSIONS.get(account_id)
        if session:
            session.stop_event.set()
            return {**session.public(), "message": "正在关闭TikTok登录窗口"}
    return {"account_id": account_id, "status": "cancelled", "message": "当前没有正在进行的TikTok登录"}


def probe_tiktok_login(account_id: int, profile_path: str) -> dict[str, Any]:
    """Verify login without uploading or clicking any publish control."""
    with _LOCK:
        running = _SESSIONS.get(account_id)
        if running and running.thread and running.thread.is_alive():
            return running.public()
    profile = ensure_profile(profile_path)
    browser = find_system_browser()
    diag = _diag_dir(account_id)
    result: dict[str, Any] = {
        "account_id": account_id,
        "profile_path": str(profile),
        "browser": str(browser),
        "browser_version": _browser_version(browser),
        "oauth_configured": False,
        "oauth_message": "TikTok官方API尚未配置",
    }
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile), executable_path=str(browser), headless=True,
                args=["--no-first-run", "--no-default-browser-check"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(TIKTOK_CHECK_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1800)
            url, title, text = page.url, page.title(), _page_text(page)
            shot = diag / f"{time.strftime('%Y%m%d-%H%M%S')}-check.png"
            page.screenshot(path=str(shot), full_page=False)
            state = classify_tiktok_page(url, text)
            if state:
                status, code, message = state
            elif authenticated_page(context.cookies(), url, _indicator_count(page)):
                status, code, message = "logged_in", "", "TikTok登录状态有效"
            elif "/login" in url.lower():
                status, code, message = "login_required", "TIKTOK_SESSION_EXPIRED", "TikTok登录状态已失效，请重新登录。"
            else:
                status, code, message = "page_changed", "TIKTOK_PAGE_CHANGED", "TikTok页面状态无法确认，请重新登录或更新软件。"
            context.close()
            result.update(status=status, error_code=code, message=message, current_url=url, page_title=title, screenshot_path=str(shot))
    except Exception as exc:
        message = str(exc)
        if "profile" in message.lower() and ("use" in message.lower() or "lock" in message.lower()):
            result.update(status="needs_manual_action", error_code="TIKTOK_PROFILE_IN_USE", message="TikTok登录窗口正在使用该账号环境，请先在登录窗口完成操作。")
        elif any(word in message.lower() for word in ("net::", "timeout", "connection", "dns")):
            result.update(status="network_error", error_code="TIKTOK_NETWORK_ERROR", message="当前无法连接TikTok，请检查海外网络环境。")
        else:
            result.update(status="page_changed", error_code="TIKTOK_PAGE_CHANGED", message="TikTok登录页面发生变化，请更新软件后重试。")
        (diag / f"{time.strftime('%Y%m%d-%H%M%S')}-check-error.json").write_text(
            json.dumps({"error_type": type(exc).__name__, "message": result["message"]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    _mark_account(account_id, str(result["status"]), "" if result["status"] == "logged_in" else str(result["message"]))
    return result
