import json
import subprocess
from pathlib import Path

from db import connect, now
from runtime_paths import MediaRuntimeError, get_ffprobe_path

BGM_EXTS = {".mp3", ".wav", ".m4a"}


def find_ffprobe(configured=""):
    try:
        return get_ffprobe_path()
    except MediaRuntimeError:
        return ""


def scan_bgm_files(folder):
    root = Path(folder)
    if not root.exists():
        return []
    return [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in BGM_EXTS]


def audio_probe(path, ffprobe_path=""):
    ffprobe = find_ffprobe(ffprobe_path)
    result = {"duration": 0.0}
    if not ffprobe:
        return result
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=20, **background_process_kwargs())
        if p.returncode == 0:
            data = json.loads(p.stdout or "{}")
            result["duration"] = float((data.get("format") or {}).get("duration") or 0)
    except Exception:
        pass
    return result


def scan_bgm_folder(folder, ffprobe_path=""):
    files = scan_bgm_files(folder)
    added = updated = skipped = 0
    with connect() as conn:
        for p in files:
            info = audio_probe(p, ffprobe_path)
            old = conn.execute("SELECT id FROM bgm_library WHERE file_path=?", (str(p),)).fetchone()
            if old:
                conn.execute(
                    "UPDATE bgm_library SET name=?,duration=? WHERE id=?",
                    (p.stem, info["duration"], old["id"]),
                )
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO bgm_library(name,file_path,duration,volume_default,created_at) VALUES(?,?,?,?,?)",
                    (p.stem, str(p), info["duration"], 0.15, now()),
                )
                added += 1
    return {"total": len(files), "added": added, "updated": updated, "skipped": skipped}
from subprocess_utils import background_process_kwargs
