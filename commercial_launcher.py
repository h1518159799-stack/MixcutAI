"""Launch the commercial React UI in a dedicated Edge desktop window."""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import threading
import shutil
from urllib.request import urlopen
from pathlib import Path

from commercial_api import CommercialHandler, FRONTEND_CONNECTED_EVENT, UPDATE_SHUTDOWN_EVENT, prepare
from activation_window import require_activation, show_expiry_reminder
from db import connect
from runtime_paths import (
    APP_HOME, DATA_DIR, RESOURCE_DIR, get_media_runtime_status,
    ensure_material_workspace, ensure_runtime_dirs,
)
from browser_profile_service import find_chromium
from splash_window import SplashWindow
from startup_diagnostics import startup_exception, startup_log
from subprocess_utils import background_process_kwargs, visible_gui_process_kwargs


APP_DIR = APP_HOME
BUILD_MARKER = "ACCOUNT_REBIND_20260912_01"
DIST_DIR = RESOURCE_DIR / "ui-commercial" / "dist"
_MUTEX_ALREADY_EXISTS = 183


def acquire_single_instance() -> int:
    """Allow only one current Mixcut launcher instance per Windows session."""
    if os.name != "nt":
        return 0
    handle = int(ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\MixcutAI.MainApplication") or 0)
    if not handle:
        raise ctypes.WinError()
    if ctypes.windll.kernel32.GetLastError() == _MUTEX_ALREADY_EXISTS:
        ctypes.windll.kernel32.CloseHandle(handle)
        return -1
    return handle


def release_single_instance(handle: int) -> None:
    if os.name == "nt" and handle > 0:
        with contextlib.suppress(Exception):
            ctypes.windll.kernel32.ReleaseMutex(handle)
            ctypes.windll.kernel32.CloseHandle(handle)


def stale_mixcut_ui_processes() -> list[int]:
    """Find orphaned Mixcut UI browser roots without matching account browsers."""
    if os.name != "nt":
        return []
    script = (
        "$all=@(Get-CimInstance Win32_Process); $byId=@{}; "
        "foreach($p in $all){$byId[[int]$p.ProcessId]=$p}; "
        "$all | Where-Object { $_.Name -match '^(chrome|msedge|chromium)\\.exe$' "
        "-and $_.CommandLine -like '*commercial-ui-chromium-profile-*' "
        "-and $_.CommandLine -like '*--app=http://127.0.0.1:*' "
        "-and $_.CommandLine -notlike '*--type=*' } | ForEach-Object { "
        "$parent=$byId[[int]$_.ParentProcessId]; "
        "if(-not $parent -or $parent.Name -ne 'MixcutAI.exe'){ $_.ProcessId } }"
    )
    command = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            **background_process_kwargs(command, "stale_mixcut_ui_scan"),
        )
        return [int(line) for line in (result.stdout or "").splitlines() if line.strip().isdigit()]
    except Exception as exc:
        startup_exception("stale_mixcut_ui_scan_error", exc)
        return []


def close_stale_mixcut_ui_processes() -> list[int]:
    """Terminate only exact orphaned Mixcut UI roots; never touch user browsers."""
    closed: list[int] = []
    detected = stale_mixcut_ui_processes()
    for pid in detected:
        handle = ctypes.windll.kernel32.OpenProcess(0x0001 | 0x00100000, False, int(pid))
        if not handle:
            startup_log("stale_mixcut_ui_close_skipped", pid=pid)
            continue
        try:
            if ctypes.windll.kernel32.TerminateProcess(handle, 0):
                ctypes.windll.kernel32.WaitForSingleObject(handle, 5000)
                closed.append(pid)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    startup_log("stale_mixcut_ui_cleanup", detected=len(detected), closed=closed)
    return closed


def show_startup_failure(exc: BaseException) -> None:
    """Expose packaged startup failures instead of silently closing the window."""
    log_path = DATA_DIR / "logs" / "startup.log"
    detail = str(exc or "").strip() or type(exc).__name__
    message = (
        "MixcutAI 启动失败。\n\n"
        f"原因：{detail[:600]}\n\n"
        f"诊断日志：{log_path}\n\n"
        "请确认完整解压客户包后，再双击 MixcutAI.exe。"
    )
    startup_log("startup_failure_dialog", reason=detail, log_path=str(log_path))
    try:
        ctypes.windll.user32.MessageBoxW(None, message, "MixcutAI 启动失败", 0x10)
    except Exception:
        # The diagnostic log remains available even if the desktop session
        # cannot create a native message box.
        pass


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_backend_health(port: int, timeout: float = 30.0) -> None:
    """Poll the real health endpoint before opening the browser window."""
    deadline = time.monotonic() + timeout
    health_url = f"http://127.0.0.1:{port}/api/health"
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with urlopen(health_url, timeout=1.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if response.status == 200 and payload.get("ok") is True:
                    startup_log("backend_healthy", port=port, url=health_url)
                    return
                last_error = "health响应异常"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.2)
    raise RuntimeError(f"本地后端健康检查超时：127.0.0.1:{port} {last_error}")


def browser_profile_is_alive(profile: Path) -> bool:
    """Detect a browser delegated to an already-running profile instance."""
    if os.name != "nt":
        return False
    escaped = str(profile).replace("'", "''")
    command = [
        "powershell", "-NoProfile", "-Command",
        "$p='" + escaped + "'; @(Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -match '^(chrome|msedge|chromium)\\.exe$' "
        "-and $_.CommandLine -like ('*'+$p+'*') } | "
        "Select-Object -ExpandProperty ProcessId).Count",
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=3,
            **background_process_kwargs(command, "browser_profile_health"),
        )
        try:
            count = int((result.stdout or "0").strip().splitlines()[-1] or "0")
        except (ValueError, IndexError):
            count = 0
        return count > 0
    except Exception as exc:
        startup_exception("browser_profile_health_error", exc, profile=str(profile))
        return False


def wait_for_browser_exit(process: subprocess.Popen, profile: Path) -> int:
    """Keep the backend alive when Chromium delegates to an existing profile process."""
    while process.poll() is None:
        if UPDATE_SHUTDOWN_EVENT.wait(0.2):
            startup_log("update_shutdown_received", browser_pid=process.pid, profile=str(profile))
            return 0
    code = int(process.returncode or 0)
    if code == 0 and browser_profile_is_alive(profile):
        startup_log("browser_delegated", pid=process.pid, profile=str(profile))
        while browser_profile_is_alive(profile):
            if UPDATE_SHUTDOWN_EVENT.wait(0.2):
                startup_log("update_shutdown_received", browser_pid=process.pid, profile=str(profile))
                return 0
            time.sleep(0.5)
    return code


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def check_media_runtime(*, show_popup: bool = True) -> None:
    startup_log("media_runtime_check_start")
    status = get_media_runtime_status(refresh=True, include_paths=True)
    startup_log(
        "media_runtime_check_end",
        ffmpeg_path=status.get("ffmpeg", {}).get("path", ""),
        ffprobe_path=status.get("ffprobe", {}).get("path", ""),
        ffmpeg_exists=status.get("ffmpeg", {}).get("exists", False),
        ffprobe_exists=status.get("ffprobe", {}).get("exists", False),
        ffmpeg_version=status.get("ffmpeg", {}).get("version", ""),
        ffprobe_version=status.get("ffprobe", {}).get("version", ""),
    )
    repair_error = ""
    try:
        from runtime_repair_service import repair_media_runtime, runtime_needs_repair
        needs_repair = runtime_needs_repair(status)
    except Exception:
        needs_repair = not status["ready"]
    if needs_repair:
        startup_log("startup_popup", reason="视频处理组件缺失或损坏，开始自动修复")
        if not show_popup:
            try:
                from runtime_repair_service import repair_media_runtime
                repair_media_runtime()
                status = get_media_runtime_status(refresh=True, include_paths=True)
            except Exception as exc:
                startup_exception("media_runtime_background_repair_error", exc)
            return
        import tkinter as tk
        from tkinter import messagebox
        notice = tk.Tk(); notice.withdraw()
        messagebox.showinfo("正在修复视频组件", "检测到视频组件缺失，正在修复...", parent=notice)
        notice.destroy()
        try:
            status = repair_media_runtime()
        except Exception as exc:
            repair_error = str(exc)
    (DATA_DIR / "preflight.json").write_text(
        json.dumps({"media_runtime": status}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if status["ready"]:
        return
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk(); root.withdraw()
    details = []
    for key in ("ffmpeg", "ffprobe"):
        item = status[key]
        label = item["name"]
        path = item.get("path") or "（未找到）"
        if not item.get("exists"):
            details.append(f"{label}不存在：\n{path}")
        elif item.get("error"):
            details.append(f"{label}检测失败：\n{path}\n{item['error']}")
        else:
            details.append(f"{label}版本：{item.get('version') or '未知'}\n路径：{path}")
    messagebox.showwarning(
        "视频处理组件异常",
        "视频处理组件自动修复失败。\n\n" + (repair_error + "\n\n" if repair_error else "") + "\n\n".join(details) + "\n\n"
        "可以进入“系统设置 → 视频处理引擎”重新检测并查看诊断状态。",
        parent=root,
    )
    startup_log("startup_popup", reason="视频组件自动修复失败", details=details, repair_error=repair_error)
    root.destroy()


def _main() -> int:
    startup_started = time.monotonic()
    splash = None
    try:
        startup_log(
            "program_start", executable=str(sys.executable), app_dir=str(APP_DIR),
            resource_dir=str(RESOURCE_DIR), data_dir=str(DATA_DIR),
            runtime_dir=str(RESOURCE_DIR / "runtime" / "ffmpeg" / "bin"),
        )
        enable_dpi_awareness()
        try:
            splash = SplashWindow()
        except Exception as exc:
            startup_exception("splash_start_error", exc)
        startup_log("runtime_dirs_start")
        ensure_runtime_dirs()
        ensure_material_workspace()
        startup_log("prepare_start")
        prepare()
        startup_log("prepare_end")
        # Startup must only establish the backend and load the UI. GPU/NVENC,
        # FFmpeg and performance probes are deferred until the relevant page
        # or generation flow requests them.
        startup_log("startup_diagnostics_deferred", checks=["gpu", "nvenc", "ffmpeg", "performance"])
        if splash:
            splash.close()
        startup_log("activation_start")
        if not require_activation():
            startup_log("activation_end", result="rejected")
            return 2
        startup_log("activation_end", result="accepted")
        startup_log("startup_update_check_deferred")
        with connect() as conn:
            value = conn.execute("SELECT value FROM app_settings WHERE key='license_expire_reminder_days'").fetchone()
        try:
            reminder_days = int(value[0]) if value else 7
        except (TypeError, ValueError):
            reminder_days = 7
        show_expiry_reminder(reminder_days)
        if not (DIST_DIR / "index.html").exists():
            raise RuntimeError(f"商业 UI 不存在：{DIST_DIR / 'index.html'}")

        browser = find_chromium()
        startup_log("browser_resolved", browser=str(browser))
        close_stale_mixcut_ui_processes()

        port = free_port()
        from http.server import ThreadingHTTPServer
        startup_log("backend_bind_start", host="127.0.0.1", port=port)
        server = ThreadingHTTPServer(("127.0.0.1", port), CommercialHandler)

        def serve_backend() -> None:
            try:
                startup_log("backend_serve_start", pid=os.getpid(), port=port)
                server.serve_forever()
            except BaseException as exc:
                startup_exception("backend_serve_error", exc, pid=os.getpid(), port=port)

        thread = threading.Thread(target=serve_backend, name="mixcut-backend", daemon=True)
        thread.start()
        wait_for_backend_health(port, timeout=30.0)
        startup_log(
            "backend_listening", pid=os.getpid(), port=port,
            elapsed_ms=round((time.monotonic() - startup_started) * 1000),
        )

        # Never reuse the legacy Edge profile: an orphaned Edge process can keep
        # that directory locked and make bundled Chromium exit immediately.
        profile_key = hashlib.sha256(str(Path(browser).resolve()).encode("utf-8")).hexdigest()[:12]
        profile = DATA_DIR / f"commercial-ui-chromium-profile-{profile_key}-{os.getpid()}"
        profile.mkdir(parents=True, exist_ok=True)
        FRONTEND_CONNECTED_EVENT.clear()
        UPDATE_SHUTDOWN_EVENT.clear()
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
        startup_log(
            "backend_command", command="embedded ThreadingHTTPServer",
            browser_command=command, pid=os.getpid(), port=port, url=url,
        )
        process = None
        try:
            process = subprocess.Popen(
                command,
                cwd=APP_DIR,
                **visible_gui_process_kwargs(command, "application_browser"),
            )
            startup_log(
                "browser_started", pid=process.pid, backend_pid=os.getpid(), port=port, url=url,
                elapsed_ms=round((time.monotonic() - startup_started) * 1000),
            )
            startup_log("media_runtime_check_deferred")
            frontend_deadline = time.monotonic() + 30.0
            while time.monotonic() < frontend_deadline:
                if FRONTEND_CONNECTED_EVENT.wait(0.2):
                    startup_log(
                        "frontend_ready", pid=process.pid, port=port,
                        elapsed_ms=round((time.monotonic() - startup_started) * 1000),
                    )
                    return wait_for_browser_exit(process, profile)
                if process.poll() is not None:
                    raise RuntimeError(f"Chromium提前退出，返回码：{process.returncode}")
            # The backend health check above is the authoritative startup gate.
            # A delayed /api/bootstrap must not close an otherwise healthy GUI;
            # keep the browser and backend alive so the frontend can finish
            # loading on slower machines.
            startup_log("frontend_ready_deferred", pid=process.pid, port=port)
            return wait_for_browser_exit(process, profile)
        finally:
            startup_log("browser_exited", pid=getattr(process, "pid", 0), port=port)
            if process is not None and process.poll() is None:
                with contextlib.suppress(Exception):
                    process.terminate()
                    process.wait(timeout=3)
            server.shutdown()
            server.server_close()
            with contextlib.suppress(Exception):
                thread.join(timeout=1)
            with contextlib.suppress(Exception):
                shutil.rmtree(profile, ignore_errors=True)
    except BaseException as exc:
        startup_exception("launcher_fatal", exc, executable=sys.executable, app_dir=str(APP_DIR))
        if splash:
            with contextlib.suppress(Exception):
                splash.close()
        show_startup_failure(exc)
        return 1


def main() -> int:
    from publishers.postflow_adapter import resolve_postflow_root, resolve_postflow_python
    postflow_root = resolve_postflow_root()
    runtime_info = dict(APP_ROOT=str(Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent), POSTFLOW_ROOT=str(postflow_root))
    try:
        runtime_info['POSTFLOW_PYTHON'] = resolve_postflow_python(postflow_root)
    except FileNotFoundError as exc:
        runtime_info['POSTFLOW_PYTHON'] = ''
        runtime_info['error'] = str(exc)
    startup_log('postflow_runtime_paths', **runtime_info)
    if '--build-info' in sys.argv:
        import publish_queue
        print(f'PUBLISH_QUEUE_BUILD_MARKER={getattr(publish_queue, "PUBLISH_QUEUE_BUILD_MARKER", "")}')
        print(f'PUBLISH_QUEUE_SOURCE_REVISION={getattr(publish_queue, "PUBLISH_QUEUE_SOURCE_REVISION", "")}')
        print(f'NORMALIZE_PLATFORM_PRESENT={hasattr(publish_queue, "normalize_platform")}')
        import types
        def constants(code):
            for value in code.co_consts:
                if isinstance(value, types.CodeType):
                    yield from constants(value)
                elif isinstance(value, str):
                    yield value
        rebind_present = any('UPDATE publish_channels SET account_id=' in value for value in constants(publish_queue.process_guarded_domestic_channel.__code__))
        print(f'ACCOUNT_REBIND_PRESENT={rebind_present}')
        print(f'PUBLISH_QUEUE_RUNTIME_FILE={getattr(publish_queue, "__file__", "")}')
        info = dict(marker=publish_queue.PUBLISH_QUEUE_BUILD_MARKER, revision=publish_queue.PUBLISH_QUEUE_SOURCE_REVISION, runtime_file=publish_queue.__file__, normalize_platform=hasattr(publish_queue, 'normalize_platform'), account_rebind=rebind_present)
        startup_log('build_info_diagnostic', **info)
        info.update(runtime_info)
        if '--build-info-output' in sys.argv:
            Path(sys.argv[sys.argv.index('--build-info-output') + 1]).write_text(json.dumps(info, ensure_ascii=False), encoding='utf-8')
        return 0
    startup_log("build_info", build_marker=BUILD_MARKER)
    import publish_queue
    startup_log("publish_queue_build_info", runtime_file=str(getattr(publish_queue, '__file__', '')), build_marker=getattr(publish_queue, 'PUBLISH_QUEUE_BUILD_MARKER', ''), source_revision=getattr(publish_queue, 'PUBLISH_QUEUE_SOURCE_REVISION', ''), normalize_platform=hasattr(publish_queue, 'normalize_platform'), account_rebind=True)
    instance_handle = acquire_single_instance()
    if instance_handle == -1:
        startup_log("duplicate_instance_blocked")
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(None, "MixcutAI 已经在运行。", "MixcutAI", 0x40)
        return 0
    try:
        return _main()
    finally:
        release_single_instance(instance_handle)


if __name__ == "__main__":
    raise SystemExit(main())
