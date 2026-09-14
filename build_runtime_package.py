from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def main() -> int:
    bin_dir, output_dir, version = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    output_dir.mkdir(parents=True, exist_ok=True)
    files = {f"bin/{name}": sha256(bin_dir / name) for name in ("ffmpeg.exe", "ffprobe.exe")}
    archive = output_dir / f"ffmpeg-v{version}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for relative in files:
            bundle.write(bin_dir / Path(relative).name, relative)
    manifest = {
        "ffmpeg_version": version,
        "url": f"https://update.ghwc.cn/runtime/{archive.name}",
        "sha256": sha256(archive),
        "size_bytes": archive.stat().st_size,
        "files": files,
    }
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    (output_dir / "runtime_manifest.json").write_text(text, encoding="utf-8")
    (bin_dir.parent / "runtime_manifest.json").write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
