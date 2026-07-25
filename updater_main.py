"""Standalone rollback-safe updater for MixcutAI.exe."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


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


def wait_for_process(pid: int, timeout_ms: int = 120000) -> bool:
    synchronize = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return True
    try:
        result = ctypes.windll.kernel32.WaitForSingleObject(handle, timeout_ms)
        return result == 0
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def launch(path: Path) -> subprocess.Popen:
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return subprocess.Popen([str(path)], cwd=str(path.parent), creationflags=subprocess.CREATE_NO_WINDOW, startupinfo=startup)


def replace_with_rollback(current: Path, new: Path, expected_sha: str, log_path: Path) -> int:
    backup = current.with_suffix(current.suffix + ".bak")
    incoming = current.with_suffix(current.suffix + ".new")
    if not current.is_file() or not new.is_file():
        raise RuntimeError("主程序或新版文件不存在")
    if sha256_file(new) != expected_sha:
        raise RuntimeError("更新器二次 SHA256 校验失败")
    incoming.unlink(missing_ok=True)
    shutil.copy2(new, incoming)
    if sha256_file(incoming) != expected_sha:
        incoming.unlink(missing_ok=True)
        raise RuntimeError("复制新版文件后校验失败")
    backup.unlink(missing_ok=True)
    shutil.copy2(current, backup)
    log(log_path, f"old exe backup: {backup}")
    try:
        os.replace(incoming, current)
        if sha256_file(current) != expected_sha:
            raise RuntimeError("替换后主程序校验失败")
        process = launch(current)
        log(log_path, f"new version started pid={process.pid}")
        time.sleep(6)
        if process.poll() is not None:
            raise RuntimeError(f"新版启动后异常退出 code={process.returncode}")
        log(log_path, "update completed")
        return 0
    except Exception:
        log(log_path, "new version failed, rolling back")
        try:
            if backup.is_file():
                os.replace(backup, current)
                launch(current)
                log(log_path, "rollback completed and old version restarted")
        except Exception as rollback_error:
            log(log_path, f"rollback failed: {rollback_error}")
            raise RuntimeError(f"更新失败且自动恢复失败：{rollback_error}")
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--current", required=True)
    parser.add_argument("--new", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--db-backup", required=True)
    parser.add_argument("--log", required=True)
    args = parser.parse_args()
    current, new = Path(args.current).resolve(), Path(args.new).resolve()
    db_backup, log_path = Path(args.db_backup).resolve(), Path(args.log).resolve()
    try:
        log(log_path, f"update requested current={current} new={new} db_backup={db_backup}")
        if not db_backup.is_file():
            raise RuntimeError("数据库备份不存在，更新已取消")
        if not wait_for_process(args.pid):
            raise RuntimeError("等待主程序关闭超时")
        return replace_with_rollback(current, new, args.sha256.lower(), log_path)
    except Exception as exc:
        log(log_path, f"update failed: {exc}")
        message("Mixcut 更新失败", str(exc), True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
