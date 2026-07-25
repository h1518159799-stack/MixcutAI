"""Playwright-based persistent login sessions for browser platform adapters."""

from __future__ import annotations

import threading
import json
from pathlib import Path
from typing import Any

from browser_profile_service import ensure_profile, find_chromium


_LOCK = threading.Lock()
_SESSIONS: dict[str, threading.Thread] = {}


def _mark_account(account_id: int, status: str, error: str = "") -> None:
    if not account_id:
        return
    from db import connect, now

    stamp = now()
    with connect() as conn:
        conn.execute(
            "UPDATE publish_accounts SET login_status=?,last_checked_at=?,last_error=?,updated_at=? WHERE id=?",
            (status, stamp, error[:500], stamp, account_id),
        )


def _is_authenticated(cookies: list[dict], domains: list[str], auth_cookie_names: list[str]) -> bool:
    names = {name.lower() for name in auth_cookie_names}
    domains = [domain.lower().lstrip(".") for domain in domains]
    for cookie in cookies:
        name = str(cookie.get("name") or "").lower()
        domain = str(cookie.get("domain") or "").lower().lstrip(".")
        if name in names and (not domains or any(domain.endswith(item) for item in domains)):
            return True
    return False


def _run_session(key: str, executable: str, profile: str, login_url: str,
                 account_id: int, domains: list[str], auth_cookie_names: list[str]) -> None:
    authenticated = False
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=profile,
                executable_path=executable,
                headless=False,
                args=["--no-first-run", "--no-default-browser-check", "--start-maximized"],
                viewport=None,
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
            # Keep the browser open only while the user is completing login.
            # HttpOnly authentication cookies are visible through Playwright.
            while True:
                page.wait_for_timeout(1200)
                if _is_authenticated(context.cookies(), domains, auth_cookie_names):
                    authenticated = True
                    _mark_account(account_id, "logged_in")
                    context.close()
                    break
    except Exception as exc:
        if not authenticated:
            # Closing the window manually is not an engine failure. Keep the
            # account actionable so the user can reopen it.
            message = str(exc)
            manual_close = "closed" in message.lower() or "target page" in message.lower()
            _mark_account(account_id, "login_required" if manual_close else "check_failed", "" if manual_close else message)
    finally:
        with _LOCK:
            _SESSIONS.pop(key, None)


def launch_persistent_login(platform_code: str, profile_path: str, login_url: str, *,
                            account_id: int = 0, cookie_domains_json: str = "[]",
                            login_cookie_names_json: str = "[]") -> dict[str, Any]:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("Playwright 未安装，请重新安装发布版") from exc
    if not str(login_url).startswith("https://"):
        raise ValueError("平台登录地址无效")
    profile = ensure_profile(profile_path)
    browser = find_chromium()
    key = str(Path(profile).resolve()).lower()
    try:
        domains = [str(item) for item in json.loads(cookie_domains_json or "[]")]
        auth_cookie_names = [str(item) for item in json.loads(login_cookie_names_json or "[]")]
    except (TypeError, ValueError):
        domains, auth_cookie_names = [], []
    if not auth_cookie_names:
        raise ValueError("平台尚未配置登录状态 Cookie")
    with _LOCK:
        running = _SESSIONS.get(key)
        if running and running.is_alive():
            return {"status": "already_open", "profile_path": str(profile), "browser": str(browser)}
        thread = threading.Thread(
            target=_run_session,
            args=(key, str(browser), str(profile), login_url, account_id, domains, auth_cookie_names),
            name=f"playwright-login-{platform_code}",
            daemon=True,
        )
        _SESSIONS[key] = thread
        thread.start()
    return {"status": "login_pending", "profile_path": str(profile), "browser": str(browser), "engine": "playwright"}
