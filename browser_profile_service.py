"""Independent Chromium profile management for publishing accounts.

No publishing automation lives here.  This module only creates isolated
profiles, opens the configured platform login page and detects saved sessions.
"""

from __future__ import annotations

import json
from contextlib import closing
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from runtime_paths import BROWSER_PROFILE_DIR, BUNDLED_CHROMIUM_EXE


CHROMIUM_CANDIDATES = (
    BUNDLED_CHROMIUM_EXE,
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
)


def safe_segment(value: str, fallback: str = "account") -> str:
    value = re.sub(r"[^\w\-\u4e00-\u9fff]+", "-", str(value or "").strip(), flags=re.UNICODE)
    return value.strip("-_.")[:60] or fallback


def profile_path(platform_code: str, account_name: str, account_id: int) -> Path:
    # Keep the public directory contract stable: {platform}/{account}.
    # Account names are unique inside a platform, so an internal id suffix is
    # unnecessary and would make the directory harder for users to recognise.
    return BROWSER_PROFILE_DIR / safe_segment(platform_code, "platform") / safe_segment(account_name)


def ensure_profile(path: str | Path) -> Path:
    root = BROWSER_PROFILE_DIR.resolve()
    target = Path(path).expanduser().resolve()
    if target != root and root not in target.parents:
        raise ValueError("浏览器目录不在允许的数据目录内")
    target.mkdir(parents=True, exist_ok=True)
    return target


def find_chromium() -> Path:
    for candidate in CHROMIUM_CANDIDATES:
        if candidate.is_file():
            return candidate
    for command in ("chrome.exe", "msedge.exe", "chromium.exe"):
        value = shutil.which(command)
        if value:
            return Path(value)
    raise FileNotFoundError("内置 Chromium 缺失，且未找到 Google Chrome 或 Microsoft Edge")


def launch_login(platform: dict[str, Any], account: dict[str, Any]) -> dict[str, str]:
    if str(platform.get("login_type") or "") != "browser_profile":
        raise ValueError("当前平台登录方式暂不支持")
    login_url = str(platform.get("login_url") or "").strip()
    if not login_url.startswith("https://"):
        raise ValueError("平台登录地址无效")
    path = ensure_profile(account["browser_profile_path"])
    browser = find_chromium()
    command = [
        str(browser),
        f"--user-data-dir={path}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        login_url,
    ]
    subprocess.Popen(command, cwd=str(browser.parent), close_fds=True)
    return {"browser": str(browser), "profile_path": str(path), "login_url": login_url}


def _json_list(value: Any) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
        return [str(item) for item in parsed if str(item).strip()] if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def _cookie_databases(profile: Path) -> list[Path]:
    candidates = [profile / "Default" / "Network" / "Cookies", profile / "Default" / "Cookies"]
    candidates.extend(profile.glob("Profile */Network/Cookies"))
    candidates.extend(profile.glob("Profile */Cookies"))
    unique: dict[str, Path] = {}
    for path in candidates:
        if path.is_file():
            unique[str(path.resolve()).lower()] = path
    return list(unique.values())


def _read_cookie_rows(database: Path, domains: list[str], cookie_names: list[str]) -> tuple[int, int]:
    temp_path: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix="mixcut-cookie-check-", suffix=".db")
        os.close(fd)
        temp_path = Path(name)
        shutil.copy2(database, temp_path)
        with closing(sqlite3.connect(temp_path)) as conn:
            rows = conn.execute("SELECT host_key,name FROM cookies").fetchall()
        relevant = [(str(host), str(name)) for host, name in rows if any(str(host).lstrip(".").endswith(domain.lstrip(".")) for domain in domains)]
        auth_names = {name.lower() for name in cookie_names}
        authenticated = sum(1 for _, name in relevant if name.lower() in auth_names)
        return len(relevant), authenticated
    except (OSError, sqlite3.Error):
        return 0, 0
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)


def detect_login_status(platform: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
    path = ensure_profile(account["browser_profile_path"])
    domains = _json_list(platform.get("cookie_domains_json"))
    cookie_names = _json_list(platform.get("login_cookie_names_json"))
    relevant = authenticated = 0
    databases = _cookie_databases(path)
    for database in databases:
        count, auth_count = _read_cookie_rows(database, domains, cookie_names)
        relevant += count
        authenticated += auth_count
    if authenticated:
        status, message = "logged_in", "已检测到平台登录会话"
    elif relevant:
        status, message = "login_required", "检测到浏览器环境，但尚未确认登录"
    elif any(path.iterdir()):
        status, message = "login_required", "浏览器环境已保存，请完成平台登录"
    else:
        status, message = "not_logged_in", "尚未创建登录会话"
    return {
        "status": status,
        "message": message,
        "cookie_count": relevant,
        "auth_cookie_count": authenticated,
        "profile_path": str(path),
    }


def open_profile_folder(path: str | Path) -> str:
    target = ensure_profile(path)
    if os.name == "nt":
        os.startfile(str(target))
    else:
        subprocess.Popen(["xdg-open", str(target)], close_fds=True)
    return str(target)


def publishing_capabilities() -> dict[str, Any]:
    """Stable extension point for the later publishing implementation."""
    return {
        "account_management": True,
        "browser_profile_login": True,
        "oauth_authorization": True,
        "token_storage": "windows-dpapi",
        "login_status_detection": True,
        "automatic_publish": False,
        "publish_api_version": "reserved-v1",
    }
