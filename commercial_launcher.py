"""Launch the commercial React UI in a dedicated Edge desktop window."""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import socket
import subprocess
import threading
from pathlib import Path

from commercial_api import CommercialHandler, prepare
from activation_window import require_activation, show_expiry_reminder
from db import connect
from runtime_paths import (
    APP_HOME, DATA_DIR, RESOURCE_DIR, get_media_runtime_status,
    ensure_material_workspace, ensure_runtime_dirs,
)
from browser_profile_service import find_chromium
from update_service import startup_update_flow
from splash_window import SplashWindow


APP_DIR = APP_HOME
DIST_DIR = RESOURCE_DIR / "ui-commercial" / "dist"
def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def check_media_runtime() -> None:
    status = get_media_runtime_status(refresh=True)
    (DATA_DIR / "preflight.json").write_text(
        json.dumps({"media_runtime": status}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if status["ready"]:
        return
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk(); root.withdraw()
    messagebox.showwarning(
        "视频处理组件异常",
        "视频处理组件缺失或损坏，请重新安装Mixcut Studio。\n\n"
        "可以进入“系统设置 → 视频处理引擎”重新检测并查看诊断状态。",
        parent=root,
    )
    root.destroy()


def main() -> int:
    enable_dpi_awareness()
    splash = None
    try:
        splash = SplashWindow()
    except Exception:
        splash = None
    ensure_runtime_dirs()
    ensure_material_workspace()
    prepare()
    if splash:
        splash.close()
    if not require_activation():
        return 2
    with connect() as conn:
        update_config = {row[0]: row[1] for row in conn.execute(
            "SELECT key,value FROM app_settings WHERE key IN ('auto_check_update','update_manifest_url')"
        ).fetchall()}
    update_result = startup_update_flow(update_config)
    if update_result == "launched":
        return 3
    if update_result == "blocked":
        return 4
    with connect() as conn:
        value = conn.execute("SELECT value FROM app_settings WHERE key='license_expire_reminder_days'").fetchone()
    try:
        reminder_days = int(value[0]) if value else 7
    except (TypeError, ValueError):
        reminder_days = 7
    show_expiry_reminder(reminder_days)
    check_media_runtime()
    if not (DIST_DIR / "index.html").exists():
        raise SystemExit("商业 UI 尚未构建，请先在 ui-commercial 目录运行 npm.cmd run build")

    browser = find_chromium()

    port = free_port()
    from http.server import ThreadingHTTPServer
    server = ThreadingHTTPServer(("127.0.0.1", port), CommercialHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # Never reuse the legacy Edge profile: an orphaned Edge process can keep
    # that directory locked and make bundled Chromium exit immediately.
    profile = DATA_DIR / "commercial-ui-chromium-profile"
    profile.mkdir(parents=True, exist_ok=True)
    url = f"http://127.0.0.1:{port}/"
    command = [
        str(browser),
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--window-size=1440,920",
        "--disable-features=msEdgeSidebarV2",
        "--no-first-run",
        "--disable-background-mode",
    ]

    try:
        process = subprocess.Popen(command, cwd=APP_DIR)
        return process.wait()
    finally:
        server.shutdown()
        server.server_close()
        with contextlib.suppress(Exception):
            thread.join(timeout=1)


if __name__ == "__main__":
    raise SystemExit(main())
