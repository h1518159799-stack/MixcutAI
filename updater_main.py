"""Standalone rollback-safe updater for MixcutAI.exe."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

from shared.update_manifest import UpdateManifest
from subprocess_utils import background_process_kwargs


UPDATER_VERSION = "1.3.27"


def log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")


def message(title: str, text: str, error: bool = False) -> None:
    flags = 0x10 if error else 0x40
    ctypes.windll.user32.MessageBoxW(None, text, title, flags)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def wait_for_process(pid: int, timeout_ms: int = 30000, log_path: Path | None = None) -> bool:
    synchronize = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        error_code = int(ctypes.windll.kernel32.GetLastError())
        if error_code == 5:
            raise PermissionError(5, "拒绝访问", f"pid={pid}")
        if log_path:
            log(log_path, f"process_exit_detected pid={pid} handle=missing winerror={error_code}")
        return True
    try:
        result = ctypes.windll.kernel32.WaitForSingleObject(handle, timeout_ms)
        exited = result == 0
        if log_path:
            log(log_path, f"process_exit_detected pid={pid} exited={exited} timeout_ms={timeout_ms}")
        return exited
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def make_file_writable(path: Path) -> None:
    if path.exists():
        path.chmod(path.stat().st_mode | stat.S_IWRITE | stat.S_IREAD)


def replace_file_with_retry(source: Path, target: Path, log_path: Path, attempts: int = 5) -> None:
    """Replace a Windows-locked file with bounded retries and diagnostics."""
    delays = (1, 2, 4, 8, 15)
    for attempt in range(attempts):
        try:
            make_file_writable(source)
            make_file_writable(target)
            os.replace(source, target)
            log(log_path, f"replace_success attempt={attempt + 1} source={source} target={target}")
            return
        except PermissionError as exc:
            source_exists = source.exists()
            target_exists = target.exists()
            log(log_path, f"replace denied attempt={attempt + 1} source={source} target={target} "
                         f"source_exists={source_exists} target_exists={target_exists} "
                         f"source_writable={os.access(source, os.W_OK) if source_exists else False} "
                         f"target_writable={os.access(target, os.W_OK) if target_exists else False} "
                         f"updater_pid={os.getpid()} winerror={getattr(exc, 'winerror', None)} error={exc}")
            if attempt + 1 >= attempts:
                raise
            time.sleep(delays[attempt])


def wait_for_file_release(path: Path, timeout_ms: int = 30000, log_path: Path | None = None) -> bool:
    """Wait until Windows allows the executable to be opened for replacement."""
    deadline = time.monotonic() + (timeout_ms / 1000)
    while True:
        try:
            make_file_writable(path)
            with path.open("r+b"):
                pass
            if log_path:
                log(log_path, f"file_release_detected path={path}")
            return True
        except FileNotFoundError:
            if log_path:
                log(log_path, f"file_release_failed path={path} reason=missing")
            return False
        except PermissionError as exc:
            if getattr(exc, "winerror", None) == 5:
                if log_path:
                    log(log_path, f"file_release_denied path={path} winerror=5 error={exc}")
                raise
            if time.monotonic() >= deadline:
                if log_path:
                    log(log_path, f"file_release_timeout path={path} winerror={getattr(exc, 'winerror', None)} error={exc}")
                return False
            time.sleep(0.5)


def remove_file_with_retry(path: Path, log_path: Path, attempts: int = 12) -> None:
    delays = (1, 1, 2, 2, 3, 3, 5, 5, 8, 8, 10, 10)
    for attempt in range(attempts):
        try:
            make_file_writable(path)
            path.unlink(missing_ok=True)
            return
        except PermissionError as exc:
            log(log_path, f"remove denied attempt={attempt + 1} path={path} updater_pid={os.getpid()} error={exc}")
            if attempt + 1 >= attempts:
                raise
            time.sleep(delays[attempt])


def restore_backup(backup: Path, current: Path, expected_sha: str, log_path: Path) -> None:
    """Restore only when the current executable is absent or contains the new build."""
    if current.is_file():
        try:
            current_sha = sha256_file(current)
        except OSError:
            current_sha = ""
        if current_sha != expected_sha:
            # The original executable was never replaced. It is already the
            # safest rollback result, so do not overwrite it with its copy.
            log(log_path, "rollback skipped: original executable is still intact")
            return
        remove_file_with_retry(current, log_path)
    if not backup.is_file():
        raise RuntimeError("旧版主程序备份不存在")
    recovery = current.with_suffix(current.suffix + ".recovery")
    recovery.unlink(missing_ok=True)
    shutil.copy2(backup, recovery)
    replace_file_with_retry(recovery, current, log_path)
    log(log_path, "rollback restored original executable")


def launch(path: Path) -> subprocess.Popen:
    command = [str(path)]
    return subprocess.Popen(command, cwd=str(path.parent),
                            **background_process_kwargs(command, "restart_application"))


def find_mixcut_ui_processes(parent_pid: int, log_path: Path) -> list[int]:
    """Find only the Chromium app window launched directly by this Mixcut process."""
    if os.name != "nt":
        return []
    script = (
        f"Get-CimInstance Win32_Process | Where-Object {{ $_.ParentProcessId -eq {int(parent_pid)} "
        "-and $_.Name -match '^(chrome|msedge|chromium)\\.exe$' "
        "-and $_.CommandLine -like '*commercial-ui-chromium-profile-*' "
        "-and $_.CommandLine -like '*--app=http://127.0.0.1:*' }} | "
        "Select-Object -ExpandProperty ProcessId"
    )
    command = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=5,
            **background_process_kwargs(command, "find_mixcut_update_ui"),
        )
        pids = [int(line.strip()) for line in (result.stdout or "").splitlines() if line.strip().isdigit()]
        log(log_path, f"mixcut_ui_processes parent_pid={parent_pid} pids={pids}")
        return pids
    except Exception as exc:
        log(log_path, f"mixcut_ui_process_scan_failed parent_pid={parent_pid} error={exc}")
        return []


def close_mixcut_ui_processes(pids: list[int], log_path: Path) -> None:
    """Close the exact stale Mixcut UI processes without touching other browsers."""
    process_terminate = 0x0001
    synchronize = 0x00100000
    for pid in pids:
        handle = ctypes.windll.kernel32.OpenProcess(process_terminate | synchronize, False, int(pid))
        if not handle:
            log(log_path, f"mixcut_ui_close_skipped pid={pid} winerror={ctypes.windll.kernel32.GetLastError()}")
            continue
        try:
            terminated = bool(ctypes.windll.kernel32.TerminateProcess(handle, 0))
            if terminated:
                ctypes.windll.kernel32.WaitForSingleObject(handle, 5000)
            log(log_path, f"mixcut_ui_closed pid={pid} success={terminated}")
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)


def replace_with_rollback(current: Path, new: Path, expected_sha: str, log_path: Path,
                          expected_size: int | None = None) -> int:
    backup = current.with_suffix(current.suffix + ".bak")
    incoming = current.with_suffix(current.suffix + ".new")
    if not current.is_file() or not new.is_file():
        raise RuntimeError("主程序或新版文件不存在")
    if expected_size is not None and new.stat().st_size != expected_size:
        raise RuntimeError("\u65b0\u7248\u6587\u4ef6\u5927\u5c0f\u4e0e update.json \u4e0d\u4e00\u81f4")
    if sha256_file(new) != expected_sha:
        raise RuntimeError("更新器二次 SHA256 校验失败")
    incoming.unlink(missing_ok=True)
    shutil.copy2(new, incoming)
    if expected_size is not None and incoming.stat().st_size != expected_size:
        incoming.unlink(missing_ok=True)
        raise RuntimeError("\u590d\u5236\u540e\u7684\u65b0\u7248\u6587\u4ef6\u5927\u5c0f\u6821\u9a8c\u5931\u8d25")
    if sha256_file(incoming) != expected_sha:
        incoming.unlink(missing_ok=True)
        raise RuntimeError("复制新版文件后校验失败")
    log(log_path, "replace_start")
    make_file_writable(backup)
    backup.unlink(missing_ok=True)
    shutil.copy2(current, backup)
    log(log_path, f"old exe backup: {backup}")
    try:
        if not wait_for_file_release(current, timeout_ms=30000, log_path=log_path):
            raise RuntimeError("MixcutAI未能正常退出，请关闭软件后重新更新")
        replace_file_with_retry(incoming, current, log_path)
        if expected_size is not None and current.stat().st_size != expected_size:
            raise RuntimeError("\u66ff\u6362\u540e\u7684\u4e3b\u7a0b\u5e8f\u5927\u5c0f\u6821\u9a8c\u5931\u8d25")
        if sha256_file(current) != expected_sha:
            raise RuntimeError("替换后主程序校验失败")
        log(log_path, "replace_success transaction_completed")
        log(log_path, "launch_new_version")
        process = launch(current)
        log(log_path, f"new version started pid={process.pid}")
        return 0
    except Exception as update_error:
        log(log_path, f"new version failed, rolling back: {update_error}")
        try:
            restore_backup(backup, current, expected_sha, log_path)
            if current.is_file():
                launch(current)
                log(log_path, "rollback completed and old version restarted")
        except Exception as rollback_error:
            log(log_path, f"rollback failed: {rollback_error}")
            raise RuntimeError(f"更新失败：{update_error}；自动恢复失败：{rollback_error}") from rollback_error
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--current", required=True)
    parser.add_argument("--new", required=True)
    parser.add_argument("--manifest", help="由主程序冻结保存的统一 update.json 快照")
    parser.add_argument("--sha256", help="旧版主程序兼容参数")
    parser.add_argument("--size", type=int, help="旧版主程序兼容参数")
    parser.add_argument("--db-backup", required=True)
    parser.add_argument("--log", required=True)
    args = parser.parse_args()
    current, new = Path(args.current).resolve(), Path(args.new).resolve()
    db_backup, log_path = Path(args.db_backup).resolve(), Path(args.log).resolve()
    try:
        log(log_path, "update_start")
        log(log_path, f"update requested current={current} new={new} db_backup={db_backup}")
        if args.manifest:
            manifest = UpdateManifest.from_file(args.manifest)
            if manifest.updater_version != UPDATER_VERSION:
                raise RuntimeError(
                    f"更新协议版本不一致：清单要求Updater {manifest.updater_version}，"
                    f"当前Updater {UPDATER_VERSION}"
                )
            expected_sha = manifest.sha256
            expected_size = manifest.file_size
            log(log_path, f"shared manifest loaded version={manifest.version} updater_version={manifest.updater_version}")
        else:
            if not args.sha256 or args.size is None:
                raise RuntimeError("缺少统一更新清单")
            expected_sha = args.sha256.lower()
            expected_size = args.size
            log(log_path, "legacy updater arguments accepted")
        if not db_backup.is_file():
            raise RuntimeError("数据库备份不存在，更新已取消")
        log(log_path, "verify_complete")
        log(log_path, "backup_complete")
        ui_processes = find_mixcut_ui_processes(args.pid, log_path)
        log(log_path, "shutdown_request")
        if not wait_for_process(args.pid, 30000, log_path):
            raise RuntimeError("MixcutAI未能正常退出，请关闭软件后重新更新")
        close_mixcut_ui_processes(ui_processes, log_path)
        return replace_with_rollback(current, new, expected_sha, log_path, expected_size)
    except Exception as exc:
        error_code = getattr(exc, "winerror", None)
        if error_code == 5:
            error_text = "更新需要管理员权限，请允许权限后重试"
        elif isinstance(exc, RuntimeError) and "未能正常退出" in str(exc):
            error_text = "MixcutAI未能正常退出，请关闭软件后重新更新"
        else:
            error_text = str(exc)
        log(log_path, f"update failed winerror={error_code} error={exc}")
        message("Mixcut 更新失败", error_text, True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
