"""Runtime paths shared by source and PyInstaller builds."""

from __future__ import annotations

import os
import sys
from pathlib import Path


FROZEN = bool(getattr(sys, "frozen", False))
SOURCE_DIR = Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", SOURCE_DIR))
EXECUTABLE_DIR = Path(sys.executable).resolve().parent if FROZEN else SOURCE_DIR

override_home = str(os.environ.get("MIXCUT_APP_HOME") or "").strip()
if override_home:
    APP_HOME = Path(override_home).expanduser().resolve()
elif FROZEN:
    local_app_data = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    APP_HOME = local_app_data / "MixcutAI"
else:
    APP_HOME = SOURCE_DIR

DATA_DIR = APP_HOME / "data"
CONFIG_DIR = APP_HOME / "config"
OUTPUT_DIR = APP_HOME / "outputs"
BROWSER_PROFILE_DIR = DATA_DIR / "browser_profiles"
BUNDLED_CHROMIUM_DIR = EXECUTABLE_DIR / "browser" / "chromium"
BUNDLED_CHROMIUM_EXE = BUNDLED_CHROMIUM_DIR / "chrome.exe"
BUNDLED_FFMPEG_DIR = EXECUTABLE_DIR / "runtime" / "ffmpeg" / "bin"
BUNDLED_FFMPEG_EXE = BUNDLED_FFMPEG_DIR / "ffmpeg.exe"
BUNDLED_FFPROBE_EXE = BUNDLED_FFMPEG_DIR / "ffprobe.exe"
MATERIAL_WORKSPACE_DIR = EXECUTABLE_DIR / "混剪素材"
DEFAULT_MATERIAL_CATEGORIES = (
    "操作过程",
    "产品",
    "产品展示",
    "店内环境",
    "服务过程",
    "空镜",
    "门头",
    "人物",
)


def ensure_runtime_dirs() -> None:
    for path in (APP_HOME, DATA_DIR, CONFIG_DIR, OUTPUT_DIR, BROWSER_PROFILE_DIR):
        path.mkdir(parents=True, exist_ok=True)


def ensure_material_workspace() -> Path:
    """Create the user-visible material folders beside MixcutAI.exe."""
    MATERIAL_WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    for category in DEFAULT_MATERIAL_CATEGORIES:
        (MATERIAL_WORKSPACE_DIR / category).mkdir(parents=True, exist_ok=True)
    return MATERIAL_WORKSPACE_DIR
