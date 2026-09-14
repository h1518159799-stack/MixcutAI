"""Secure online update client for Mixcut AI.

This module is isolated from video, TTS, timeline and licensing code. It only
checks an HTTPS manifest, stages a verified EXE and starts MixcutUpdater.exe.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from db import DB_PATH
from runtime_paths import DATA_DIR, RESOURCE_DIR, SOURCE_DIR
from shared.update_manifest import UpdateManifest, UpdateManifestError
from subprocess_utils import background_process_kwargs


MAX_MANIFEST_BYTES = 64 * 1024
MAX_UPDATE_BYTES = 600 * 1024 * 1024
MAX_UPDATER_BYTES = 128 * 1024 * 1024
UPDATE_DIR = DATA_DIR / "updates"
BACKUP_DIR = DATA_DIR / "backups"


class UpdateError(RuntimeError):
    pass


def current_version() -> str:
    candidates = (RESOURCE_DIR / "default_config.json", SOURCE_DIR / "default_config.json")
    for path in candidates:
        try:
            value = json.loads(path.read_text(encoding="utf-8")).get("app_version")
            if value:
                return str(value).strip()
        except (OSError, ValueError, TypeError):
            continue
    return "1.3.27"


def _version_tuple(value: str) -> tuple[int, ...]:
    text = str(value or "").strip().lower().lstrip("v")
    core = text.split("-", 1)[0]
    parts = core.split(".")
    if not parts or any(not part.isdigit() for part in parts):
        raise UpdateError(f"无效版本号：{value}")
    return tuple(int(part) for part in parts)


def is_newer_version(remote: str, local: str | None = None) -> bool:
    left = list(_version_tuple(remote))
    right = list(_version_tuple(local or current_version()))
    size = max(len(left), len(right))
    left.extend([0] * (size - len(left)))
    right.extend([0] * (size - len(right)))
    return tuple(left) > tuple(right)


def _require_https(url: str, label: str) -> str:
    value = str(url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise UpdateError(f"{label}必须使用有效的 HTTPS 地址")
    return value


def _open_https(url: str, timeout: int):
    request = urllib.request.Request(
        _require_https(url, "更新地址"),
        headers={"Accept": "application/json, application/octet-stream", "User-Agent": f"MixcutAI/{current_version()}"},
        method="GET",
    )
    response = urllib.request.urlopen(request, timeout=max(3, min(60, int(timeout))))
    final_url = response.geturl()
    if urlparse(final_url).scheme.lower() != "https":
        response.close()
        raise UpdateError("更新服务器重定向到了非 HTTPS 地址")
    return response


def fetch_update_manifest(url: str, timeout: int = 8) -> UpdateManifest:
    try:
        with _open_https(url, timeout) as response:
            content_length = int(response.headers.get("Content-Length") or 0)
            if content_length > MAX_MANIFEST_BYTES:
                raise UpdateError("update.json 文件过大")
            raw = response.read(MAX_MANIFEST_BYTES + 1)
    except UpdateError:
        raise
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"更新服务器返回 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise UpdateError(f"无法连接更新服务器：{exc.reason}") from exc
    if len(raw) > MAX_MANIFEST_BYTES:
        raise UpdateError("update.json 文件过大")
    try:
        manifest = UpdateManifest.from_json_bytes(raw)
    except UpdateManifestError as exc:
        raise UpdateError(str(exc)) from exc
    if manifest.file_size > MAX_UPDATE_BYTES:
        raise UpdateError("更新清单中的 file_size 超出允许范围")
    if manifest.updater_artifact and manifest.updater_artifact.file_size > MAX_UPDATER_BYTES:
        raise UpdateError("更新器文件大小超出允许范围")
    return manifest


def check_for_update(manifest_url: str, local_version: str | None = None, timeout: int = 8) -> dict[str, Any]:
    manifest = fetch_update_manifest(manifest_url, timeout)
    current = local_version or current_version()
    return {
        "current_version": current,
        "available": is_newer_version(manifest.version, current),
        "manifest": manifest.public_dict(),
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


DownloadProgress = Callable[[int, int], None]
UpdateStage = Callable[[str], None]


def update_log(message: str) -> None:
    """Write a short client-side update trace without exposing credentials."""
    path = DATA_DIR / "update.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")


def download_update(
    manifest: UpdateManifest,
    timeout: int = 60,
    progress_callback: DownloadProgress | None = None,
) -> Path:
    update_log("download_start")
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    target = UPDATE_DIR / f"MixcutAI-{manifest.version}.exe"
    partial = target.with_suffix(".exe.part")
    if target.is_file() and target.stat().st_size == manifest.size_bytes and sha256_file(target) == manifest.sha256:
        update_log("download_complete cached=true")
        if progress_callback:
            size = target.stat().st_size
            progress_callback(size, size)
        return target
    partial.unlink(missing_ok=True)
    downloaded = 0
    try:
        with _open_https(manifest.download_url, timeout) as response, partial.open("wb") as output:
            content_length = int(response.headers.get("Content-Length") or 0)
            if content_length > MAX_UPDATE_BYTES:
                raise UpdateError("新版安装文件超过大小限制")
            if content_length and content_length != manifest.size_bytes:
                raise UpdateError("\u65b0\u7248\u5b89\u88c5\u6587\u4ef6\u5927\u5c0f\u4e0e update.json \u4e0d\u4e00\u81f4")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > MAX_UPDATE_BYTES:
                    raise UpdateError("新版安装文件超过大小限制")
                output.write(chunk)
                if progress_callback:
                    progress_callback(downloaded, content_length)
            output.flush()
            os.fsync(output.fileno())
    except UpdateError:
        partial.unlink(missing_ok=True)
        raise
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        partial.unlink(missing_ok=True)
        raise UpdateError(f"新版下载失败：{exc}") from exc
    actual = sha256_file(partial)
    if partial.stat().st_size != manifest.size_bytes:
        partial.unlink(missing_ok=True)
        raise UpdateError("\u65b0\u7248\u5b89\u88c5\u6587\u4ef6\u5927\u5c0f\u6821\u9a8c\u5931\u8d25")
    if actual != manifest.sha256:
        partial.unlink(missing_ok=True)
        raise UpdateError(f"新版 SHA256 校验失败，期望 {manifest.sha256}，实际 {actual}")
    os.replace(partial, target)
    update_log(f"download_complete size={target.stat().st_size}")
    update_log("verify_complete")
    return target


def backup_database() -> Path:
    if not DB_PATH.is_file():
        raise UpdateError("更新前找不到用户数据库")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"mixcut-before-update-{stamp}.db"
    try:
        shutil.copy2(DB_PATH, target)
    except OSError as exc:
        raise UpdateError(f"更新前数据库备份失败：{exc}") from exc
    if not target.is_file() or target.stat().st_size != DB_PATH.stat().st_size:
        raise UpdateError("更新前数据库备份校验失败")
    update_log(f"backup_complete path={target}")
    return target


def get_updater_path() -> Path:
    """Resolve the updater beside the real executable, never relative to cwd."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "MixcutUpdater.exe"
    return SOURCE_DIR / "release" / "MixcutUpdater.exe"


def updater_executable() -> Path:
    """Backward-compatible alias for callers and integrations."""
    return get_updater_path()


def ensure_updater(manifest: UpdateManifest) -> Path:
    """Ensure the updater beside the app matches the signed manifest metadata."""
    updater = get_updater_path()
    has_metadata = bool(manifest.updater_url and manifest.updater_sha256 and manifest.updater_size_bytes)
    if updater.is_file() and not has_metadata:
        return updater
    if updater.is_file() and has_metadata:
        try:
            if updater.stat().st_size == manifest.updater_size_bytes and sha256_file(updater) == manifest.updater_sha256:
                return updater
        except OSError:
            pass
    if not has_metadata:
        raise UpdateError("更新组件缺失，请下载完整客户端后重试")

    # Keep both temporary files beside the final updater so os.replace remains
    # atomic even when the application is installed on a different drive than
    # the data/cache directory.
    updater.parent.mkdir(parents=True, exist_ok=True)
    partial = updater.with_name("MixcutUpdater.exe.part")
    staged = updater.with_name("MixcutUpdater.exe.staged")
    partial.unlink(missing_ok=True)
    try:
        with _open_https(manifest.updater_url, 60) as response, partial.open("wb") as output:
            content_length = int(response.headers.get("Content-Length") or 0)
            if content_length and content_length != manifest.updater_size_bytes:
                raise UpdateError("更新器下载大小与清单不一致")
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPDATER_BYTES:
                    raise UpdateError("更新器文件超过大小限制")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if total != manifest.updater_size_bytes or sha256_file(partial) != manifest.updater_sha256:
            raise UpdateError("更新器文件校验失败")
        os.replace(partial, staged)
        os.replace(staged, updater)
        if (
            not updater.is_file()
            or updater.stat().st_size != manifest.updater_size_bytes
            or sha256_file(updater) != manifest.updater_sha256
        ):
            raise UpdateError("更新器安装后校验失败")
        return updater
    except UpdateError:
        partial.unlink(missing_ok=True)
        staged.unlink(missing_ok=True)
        raise
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        partial.unlink(missing_ok=True)
        staged.unlink(missing_ok=True)
        raise UpdateError(f"更新器恢复失败：{exc}") from exc


def launch_updater(new_exe: Path, manifest: UpdateManifest, database_backup: Path) -> dict[str, str]:
    updater = get_updater_path()
    if not updater.is_file():
        raise UpdateError("更新组件缺失，请下载完整客户端后重试")
    current_exe = Path(sys.executable).resolve() if getattr(sys, "frozen", False) else SOURCE_DIR / "release" / "MixcutAI.exe"
    if not current_exe.is_file():
        raise UpdateError("找不到当前主程序 EXE")
    log_path = DATA_DIR / "update.log"
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    manifest_snapshot = UPDATE_DIR / f"update-manifest-{manifest.version}.json"
    staged_manifest = manifest_snapshot.with_suffix(".json.part")
    try:
        staged_manifest.write_text(manifest.to_json(), encoding="utf-8")
        os.replace(staged_manifest, manifest_snapshot)
    except OSError as exc:
        staged_manifest.unlink(missing_ok=True)
        raise UpdateError(f"无法保存统一更新清单：{exc}") from exc
    update_log("shutdown_request")
    command = [
        str(updater),
        "--pid", str(os.getpid()),
        "--current", str(current_exe),
        "--new", str(new_exe),
        "--manifest", str(manifest_snapshot),
        "--db-backup", str(database_backup),
        "--log", str(log_path),
    ]
    try:
        process = subprocess.Popen(command, cwd=str(current_exe.parent),
                                   **background_process_kwargs(command, "launch_updater"))
    except OSError as exc:
        update_log(f"updater_launch_failed winerror={getattr(exc, 'winerror', None)} error={exc}")
        raise UpdateError(f"无法启动更新器：{exc}") from exc
    update_log(f"updater_started pid={process.pid}")
    return {
        "version": manifest.version,
        "staged_path": str(new_exe),
        "database_backup": str(database_backup),
        "updater_pid": str(process.pid),
    }


def stage_and_launch(
    manifest: UpdateManifest,
    progress_callback: DownloadProgress | None = None,
    status_callback: UpdateStage | None = None,
) -> dict[str, str]:
    if status_callback:
        status_callback("准备下载更新组件...")
    ensure_updater(manifest)
    if status_callback:
        status_callback("正在下载新版程序...")
    new_exe = download_update(manifest, progress_callback=progress_callback)
    if status_callback:
        status_callback("正在备份用户数据...")
    database_backup = backup_database()
    if status_callback:
        status_callback("正在通知软件退出...")
    return launch_updater(new_exe, manifest, database_backup)


def configured_manifest_url(config: dict[str, Any]) -> str:
    return str(config.get("update_manifest_url") or "").strip()


def _startup_update_flow(config: dict[str, Any]) -> str:
    if str(config.get("auto_check_update", "1")).lower() not in ("1", "true", "on", "yes"):
        return "continue"
    url = configured_manifest_url(config)
    if not url:
        return "continue"
    try:
        result = check_for_update(url, timeout=6)
    except UpdateError:
        return "continue"
    if not result["available"]:
        return "continue"
    manifest = UpdateManifest.from_dict(result["manifest"])
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk(); root.withdraw()
    message = f"{manifest.title}\n\n版本：v{manifest.version}\n\n{manifest.content}"
    if manifest.force_update:
        accepted = messagebox.askokcancel("必须更新", message + "\n\n该版本为强制更新，取消后软件将退出。", parent=root)
    else:
        accepted = messagebox.askyesno("发现新版本", message + "\n\n现在下载并更新吗？", parent=root)
    root.destroy()
    if not accepted:
        return "blocked" if manifest.force_update else "continue"
    progress_root = tk.Tk()
    progress_root.title("正在更新 MixcutAI")
    progress_root.resizable(False, False)
    from tkinter import ttk
    frame = ttk.Frame(progress_root, padding=18)
    frame.pack(fill="both", expand=True)
    label = ttk.Label(frame, text="准备更新...")
    label.pack(anchor="w", pady=(0, 10))
    progress = ttk.Progressbar(frame, length=380, mode="determinate", maximum=100)
    progress.pack(fill="x")
    detail = ttk.Label(frame, text="0%")
    detail.pack(anchor="e", pady=(8, 0))
    progress_root.update_idletasks()
    width, height = 430, 115
    x = max(0, (progress_root.winfo_screenwidth() - width) // 2)
    y = max(0, (progress_root.winfo_screenheight() - height) // 2)
    progress_root.geometry(f"{width}x{height}+{x}+{y}")

    def report(downloaded: int, total: int) -> None:
        if total > 0:
            percent = min(100, int(downloaded * 100 / total))
            progress["value"] = percent
            detail.configure(text=f"{percent}%  {downloaded / 1048576:.1f} / {total / 1048576:.1f} MB")
        else:
            progress.configure(mode="indeterminate")
            progress.start(12)
            detail.configure(text=f"已下载 {downloaded / 1048576:.1f} MB")
        label.configure(text="正在下载新版程序，请勿关闭软件")
        progress_root.update_idletasks()
        progress_root.update()

    try:
        def set_stage(text: str) -> None:
            label.configure(text=text)
            detail.configure(text="请稍候")
            progress_root.update_idletasks()
            progress_root.update()

        stage_and_launch(manifest, progress_callback=report, status_callback=set_stage)
        progress_root.destroy()
        return "launched"
    except UpdateError as exc:
        progress_root.destroy()
        error_root = tk.Tk(); error_root.withdraw()
        messagebox.showerror("更新失败", str(exc), parent=error_root)
        error_root.destroy()
        return "blocked" if manifest.force_update else "continue"


def startup_update_flow(config: dict[str, Any]) -> str:
    """Run the optional updater without ever blocking normal application startup."""
    try:
        return _startup_update_flow(config)
    except Exception:
        return "continue"

