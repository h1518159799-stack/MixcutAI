"""Launch the commercial React UI in a dedicated Edge desktop window."""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import shutil
import socket
import subprocess
import threading
from pathlib import Path

from commercial_api import CommercialHandler, prepare
from activation_window import require_activation, show_expiry_reminder
from db import connect
from runtime_paths import (
    APP_HOME, DATA_DIR, RESOURCE_DIR, BUNDLED_FFMPEG_EXE,
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


def warn_missing_ffmpeg() -> None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key='ffmpeg_path'").fetchone()
    configured = str(row[0] if row else "").strip()
    available = (
        bool(configured and Path(configured).is_file())
        or BUNDLED_FFMPEG_EXE.is_file()
        or bool(shutil.which("ffmpeg"))
    )
    (DATA_DIR / "preflight.json").write_text(
        json.dumps({"ffmpeg_available": available, "configured_path": configured}, ensure_ascii=False),
        encoding="utf-8",
    )
    if available:
        return
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk(); root.withdraw()
    messagebox.showwarning(
        "FFmpeg 尚未配置",
        "当前没有检测到 FFmpeg。软件界面可以正常启动，但生成语音转码、字幕烧录和最终成片前，"
        "请在“系统设置”中填写 ffmpeg.exe 和 ffprobe.exe 的完整路径。",
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
    warn_missing_ffmpeg()
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
