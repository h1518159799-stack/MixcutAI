"""Validate and normalize user-selected publish cover images."""
from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from runtime_paths import DATA_DIR

PUBLISH_COVER_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def normalize_cover_path(value: str) -> str:
    raw = str(value or "").strip().strip('"')
    if not raw:
        return ""
    source = Path(raw).expanduser()
    if not source.is_file():
        raise ValueError("封面文件不存在，请重新选择")
    source = source.resolve()
    if source.suffix.lower() in PUBLISH_COVER_EXTS:
        return str(source)

    # BMP/JFIF/TIFF and other Pillow-readable images are normalized to PNG so
    # every publisher receives a predictable local file type.
    try:
        with Image.open(source) as image:
            image.verify()
        with Image.open(source) as image:
            normalized = ImageOps.exif_transpose(image).convert("RGB")
            target_dir = DATA_DIR / "publish_covers"
            target_dir.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(str(source).encode("utf-8") + str(source.stat().st_mtime_ns).encode()).hexdigest()[:16]
            target = target_dir / f"cover_{digest}.png"
            normalized.save(target, "PNG", optimize=True)
            return str(target)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError("封面格式无法识别，请选择 JPG、PNG、WEBP、BMP 或 TIFF 图片") from exc

