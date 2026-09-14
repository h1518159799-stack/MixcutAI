"""Cross-platform subprocess options and diagnostics for background tools."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Sequence

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_LOG_LOCK = threading.Lock()
_SECRET_RE = re.compile(
    r"(?i)(authorization|bearer|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|"
    r"client[_ -]?secret|cookie|password)(\s*[:=]?\s*)([^\s,;]+)"
)


def process_launch_log_path() -> Path:
    override = os.environ.get("MIXCUT_PROCESS_LAUNCH_LOG", "").strip()
    if override:
        return Path(override)
    local = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    return local / "MixcutAI" / "data" / "logs" / "process-launch.log"


def _safe_command(command: Sequence[object] | str | None) -> str:
    if command is None:
        return ""
    if isinstance(command, str):
        value = command
    else:
        parts: list[str] = []
        hide_next = False
        for raw in command:
            item = str(raw)
            if hide_next:
                parts.append("<redacted>")
                hide_next = False
                continue
            if item.lower() in {"-encodedcommand", "-enc", "--password", "--token", "--api-key"}:
                parts.append(item)
                hide_next = True
                continue
            parts.append(item[:300])
        value = " ".join(parts)
    return _SECRET_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}<redacted>", value)[:3000]


def record_process_launch(command: Sequence[object] | str | None, stage: str, hidden_window: bool) -> None:
    """Append a redacted JSONL record without ever interrupting the caller."""
    payload = {
        "command": _safe_command(command),
        "stage": str(stage or "external_process"),
        "hidden_window": bool(hidden_window),
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    try:
        path = process_launch_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_LOCK, path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


def infer_process_stage(command: Sequence[object] | str | None, fallback: str = "external_process") -> str:
    first = ""
    if isinstance(command, str):
        first = command.split(maxsplit=1)[0] if command.strip() else ""
    elif command:
        first = str(command[0])
    name = Path(first).name.lower()
    if "ffprobe" in name:
        return "ffprobe_validation"
    if "ffmpeg" in name:
        return "ffmpeg_render"
    if "nvidia-smi" in name:
        return "gpu_detection"
    if name in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        return "powershell_helper"
    return fallback


def background_process_kwargs(
    command: Sequence[object] | str | None = None,
    stage: str = "external_process",
) -> dict:
    """Prevent external helpers from opening console windows and record the launch."""
    hidden = os.name == "nt"
    record_process_launch(command, stage, hidden)
    if not hidden:
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


def visible_gui_process_kwargs(
    command: Sequence[object] | str | None = None,
    stage: str = "desktop_application",
) -> dict:
    """Suppress a console window without hiding the external GUI itself."""
    hidden_console = os.name == "nt"
    record_process_launch(command, stage, hidden_console)
    if not hidden_console:
        return {}
    # STARTUPINFO(SW_HIDE) must not be used here: it also hides Chromium's
    # application window and makes a healthy launch look like a silent exit.
    return {"creationflags": CREATE_NO_WINDOW}
