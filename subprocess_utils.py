"""Cross-platform subprocess options for background media tools."""

from __future__ import annotations

import os
import subprocess


def background_process_kwargs() -> dict:
    """Prevent FFmpeg, FFprobe and PowerShell helpers from opening console windows."""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }
