from __future__ import annotations

from pathlib import Path

from bgm_service import audio_probe
from db import connect, now

SFX_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg"}
KEYWORD_PRESETS = {
    "shine": "展示,新品,升级,亮相,产品,高光",
    "whoosh": "切换,转场,进入,移动,快速",
    "click": "点击,选择,确认,按钮,操作",
    "machine": "生产,机器,设备,工厂,车间,制造",
}


def infer_category(name: str) -> str:
    value = name.lower()
    for category in KEYWORD_PRESETS:
        if category in value:
            return category
    return "general"


def scan_sfx_folder(folder: str, ffprobe_path: str = "") -> dict:
    root = Path(folder)
    if not root.is_dir():
        raise ValueError("音效目录不存在")
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SFX_EXTS]
    added = updated = 0
    with connect() as conn:
        for path in files:
            info = audio_probe(path, ffprobe_path)
            category = infer_category(path.stem)
            keywords = KEYWORD_PRESETS.get(category, path.stem.replace("_", ",").replace("-", ","))
            old = conn.execute("SELECT id FROM sound_effect_library WHERE file_path=?", (str(path),)).fetchone()
            if old:
                conn.execute(
                    "UPDATE sound_effect_library SET name=?,category=?,keywords=?,duration=?,updated_at=? WHERE id=?",
                    (path.stem, category, keywords, info["duration"], now(), old["id"]),
                )
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO sound_effect_library(name,category,keywords,file_path,duration,volume_default,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (path.stem, category, keywords, str(path), info["duration"], 0.25, now(), now()),
                )
                added += 1
    return {"total": len(files), "added": added, "updated": updated, "folder": str(root)}

