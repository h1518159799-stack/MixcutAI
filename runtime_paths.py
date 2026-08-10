"""Runtime paths shared by source and PyInstaller builds.

FFmpeg and FFprobe are application runtime components, not user settings.
Release builds resolve only files shipped beside the executable.  Source runs
prefer the project vendor directory and may fall back to PATH for developers.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path


FROZEN = bool(getattr(sys, "frozen", False))
SOURCE_DIR = Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", SOURCE_DIR)).resolve()
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
LOG_DIR = DATA_DIR / "logs"
TASK_LOG_DIR = LOG_DIR / "tasks"
BROWSER_PROFILE_DIR = DATA_DIR / "browser_profiles"
BUNDLED_CHROMIUM_DIR = EXECUTABLE_DIR / "browser" / "chromium"
BUNDLED_CHROMIUM_EXE = BUNDLED_CHROMIUM_DIR / "chrome.exe"
BUNDLED_FFMPEG_DIR = EXECUTABLE_DIR / "runtime" / "ffmpeg" / "bin"
BUNDLED_FFMPEG_EXE = BUNDLED_FFMPEG_DIR / "ffmpeg.exe"
BUNDLED_FFPROBE_EXE = BUNDLED_FFMPEG_DIR / "ffprobe.exe"
MATERIAL_WORKSPACE_DIR = EXECUTABLE_DIR / "混剪素材"
DEFAULT_MATERIAL_CATEGORIES = (
    "操作过程", "产品", "产品展示", "店内环境", "服务过程", "空镜", "门头", "人物",
)

_MEDIA_LOCK = threading.RLock()
_MEDIA_STATUS_CACHE: dict | None = None
_VERSION_RE = re.compile(r"\bversion\s+([^\s]+)", re.IGNORECASE)


class MediaRuntimeError(RuntimeError):
    """A required bundled media component is missing or cannot execute."""


def ensure_runtime_dirs() -> None:
    for path in (APP_HOME, DATA_DIR, CONFIG_DIR, OUTPUT_DIR, BROWSER_PROFILE_DIR, LOG_DIR, TASK_LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)


def ensure_material_workspace() -> Path:
    """Create the user-visible material folders beside MixcutAI.exe."""
    MATERIAL_WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    for category in DEFAULT_MATERIAL_CATEGORIES:
        (MATERIAL_WORKSPACE_DIR / category).mkdir(parents=True, exist_ok=True)
    return MATERIAL_WORKSPACE_DIR


def _dedupe(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = os.path.normcase(os.path.abspath(str(path)))
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _bundled_candidates(binary_name: str) -> list[Path]:
    """Return runtime-relative candidates without consulting persisted settings."""
    roots = [
        EXECUTABLE_DIR / "runtime" / "ffmpeg" / "bin",
        RESOURCE_DIR / "runtime" / "ffmpeg" / "bin",
        # Compatibility with older onedir layouts.  These remain relative to
        # the running executable/resource root and never contain a drive path.
        EXECUTABLE_DIR / "resources" / "backend" / "_internal" / "static" / "ffmpeg" / "bin",
        RESOURCE_DIR / "static" / "ffmpeg" / "bin",
    ]
    if not FROZEN:
        roots.insert(0, SOURCE_DIR / "vendor" / "ffmpeg" / "bin")
    return _dedupe([root / binary_name for root in roots])


def _resolve_media_binary(kind: str) -> str:
    normalized = str(kind or "").strip().lower().replace(".exe", "")
    if normalized not in {"ffmpeg", "ffprobe"}:
        raise ValueError(f"不支持的媒体组件：{kind}")
    binary_name = normalized + (".exe" if os.name == "nt" else "")
    for candidate in _bundled_candidates(binary_name):
        if candidate.is_file():
            return str(candidate.resolve())
    if not FROZEN:
        found = shutil.which(normalized) or shutil.which(binary_name)
        if found and Path(found).is_file():
            return str(Path(found).resolve())
    raise MediaRuntimeError("视频处理组件缺失或损坏，请重新安装Mixcut Studio。")


def get_ffmpeg_path(*_legacy_args, **_legacy_kwargs) -> str:
    """Resolve FFmpeg. Legacy configured-path arguments are intentionally ignored."""
    return _resolve_media_binary("ffmpeg")


def get_ffprobe_path(*_legacy_args, **_legacy_kwargs) -> str:
    """Resolve FFprobe. Legacy configured-path arguments are intentionally ignored."""
    return _resolve_media_binary("ffprobe")


def _component_status(kind: str) -> dict:
    label = "FFmpeg" if kind == "ffmpeg" else "FFprobe"
    try:
        path = get_ffmpeg_path() if kind == "ffmpeg" else get_ffprobe_path()
    except MediaRuntimeError as exc:
        return {"name": label, "ok": False, "version": "", "bundled": FROZEN, "error": str(exc), "path": ""}
    try:
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        completed = subprocess.run(
            [path, "-version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, startupinfo=startupinfo, creationflags=creationflags,
        )
        first_line = ((completed.stdout or completed.stderr or "").splitlines() or [""])[0]
        match = _VERSION_RE.search(first_line)
        version = match.group(1) if match else "未知版本"
        if completed.returncode != 0:
            raise OSError(f"exit={completed.returncode}")
        return {"name": label, "ok": True, "version": version, "bundled": FROZEN or "runtime" in Path(path).parts, "error": "", "path": path}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"name": label, "ok": False, "version": "", "bundled": FROZEN, "error": f"组件无法运行：{type(exc).__name__}", "path": path}


def get_media_runtime_status(*, refresh: bool = False, include_paths: bool = False) -> dict:
    """Validate both binaries and return a UI-safe status by default."""
    global _MEDIA_STATUS_CACHE
    with _MEDIA_LOCK:
        if refresh or _MEDIA_STATUS_CACHE is None:
            ffmpeg = _component_status("ffmpeg")
            ffprobe = _component_status("ffprobe")
            ready = bool(ffmpeg["ok"] and ffprobe["ok"])
            _MEDIA_STATUS_CACHE = {
                "ready": ready,
                "state": "ready" if ready else "error",
                "message": "视频处理组件已内置，无需配置。" if ready else "视频处理组件缺失或损坏，请重新安装Mixcut Studio。",
                "ffmpeg": ffmpeg,
                "ffprobe": ffprobe,
            }
            _write_runtime_diagnostic(_MEDIA_STATUS_CACHE)
        result = json.loads(json.dumps(_MEDIA_STATUS_CACHE, ensure_ascii=False))
    if not include_paths:
        result["ffmpeg"].pop("path", None)
        result["ffprobe"].pop("path", None)
    return result


def _write_runtime_diagnostic(status: dict) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / "media-runtime.json").write_text(
            json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    except OSError:
        pass
