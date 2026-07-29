from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import json
import os
import subprocess
import time
from pathlib import Path

from PIL import ImageGrab


def visible_mixcut_windows() -> list[dict]:
    user32 = ctypes.windll.user32
    results: list[dict] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd, _):
        process_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        if "MixcutAI" not in title.value and "MIXCUT" not in title.value:
            return True
        results.append({
            "hwnd": int(hwnd), "pid": process_id.value, "title": title.value,
            "rect": [rect.left, rect.top, rect.right, rect.bottom],
        })
        return True

    user32.EnumWindows(callback, 0)
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--local-app-data", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    args.local_app_data.mkdir(parents=True, exist_ok=True)
    args.evidence.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(args.local_app_data.resolve())
    process = subprocess.Popen([str(args.exe.resolve())], cwd=str(args.exe.resolve().parent), env=env)
    deadline = time.time() + 50
    windows: list[dict] = []
    while time.time() < deadline and process.poll() is None:
        windows = visible_mixcut_windows()
        if any("MixcutAI" in item["title"] or "MIXCUT" in item["title"] for item in windows):
            break
        time.sleep(1)
    result = {
        "pid": process.pid,
        "alive": process.poll() is None,
        "exit_code": process.poll(),
        "windows": windows,
        "local_app_data": str(args.local_app_data.resolve()),
    }
    visible = next((item for item in windows if item["rect"][2] > item["rect"][0] and item["rect"][3] > item["rect"][1]), None)
    if visible:
        ctypes.windll.user32.ShowWindow(visible["hwnd"], 9)
        ctypes.windll.user32.SetForegroundWindow(visible["hwnd"])
        time.sleep(1)
        ImageGrab.grab(window=visible["hwnd"], include_layered_windows=True).save(args.evidence / "activation_window.png")
        ctypes.windll.user32.PostMessageW(visible["hwnd"], 0x0010, 0, 0)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.terminate()
        process.wait(timeout=10)
    app_home = args.local_app_data / "MixcutAI"
    result["created_paths"] = sorted(str(item.relative_to(args.local_app_data)) for item in app_home.rglob("*") if item.is_dir()) if app_home.exists() else []
    result["database_exists"] = (app_home / "data" / "mixcut.db").is_file()
    result["final_exit_code"] = process.returncode
    (args.evidence / "activation_smoke.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["alive"] and visible and result["database_exists"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
