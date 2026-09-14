"""Create and verify the minimal Windows user release archive.

Kept in a UTF-8 Python source file instead of piping Python through Windows
PowerShell 5, whose legacy pipe encoding corrupts Chinese archive names.
"""

from __future__ import annotations

import argparse
import pathlib
import zipfile


MATERIAL_CATEGORIES = (
    "操作过程",
    "产品",
    "产品展示",
    "店内环境",
    "服务过程",
    "空镜",
    "门头",
    "人物",
)

ALLOWED_ROOT = {
    "MixcutAI.exe",
    "MixcutUpdater.exe",
    "default_config.json",
    "使用说明.txt",
    "CHANGELOG.txt",
    "LICENSE.txt",
    "THIRD_PARTY_NOTICES.txt",
    "VERSION.txt",
}

FORBIDDEN_MARKERS = (
    ".py",
    ".db",
    ".log",
    ".env",
    ".key",
    ".pem",
    "license_admin",
    "production_test",
    "test_",
)


def _excluded(relative: str) -> bool:
    lowered = relative.lower()
    # Chromium may create debug.log in its own installation directory after a
    # local smoke test.  It is runtime noise and must not enter customer ZIPs.
    return lowered.endswith(".log") or "/cache/" in f"/{lowered}/"


def create_archive(stage_value: str, target_value: str) -> None:
    stage = pathlib.Path(stage_value).resolve()
    target = pathlib.Path(target_value).resolve()
    material_root = stage / "混剪素材"
    for category in MATERIAL_CATEGORIES:
        (material_root / category).mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(stage.rglob("*"), key=lambda item: item.as_posix()):
            relative = path.relative_to(stage).as_posix()
            if _excluded(relative):
                continue
            if path.is_dir():
                archive.writestr(relative.rstrip("/") + "/", b"")
            else:
                archive.write(path, relative)


def verify_archive(target_value: str) -> None:
    target = pathlib.Path(target_value).resolve()
    with zipfile.ZipFile(target) as archive:
        names = [item.filename.replace("\\", "/").strip("/") for item in archive.infolist() if not item.is_dir()]
        bad_members = archive.testzip()
    if bad_members:
        raise SystemExit(f"release ZIP CRC check failed: {bad_members}")
    if 'runtime/postflow/postflow_confirmation.py' not in names:
        raise SystemExit('POSTFLOW_CONFIRMATION_MISSING')
    unexpected = [
        name
        for name in names
        if name not in ALLOWED_ROOT
        and not (name.startswith("browser/chromium/") or name.startswith("runtime/browser/chromium/"))
        and not name.startswith("runtime/ffmpeg/")
        and not name.startswith("runtime/postflow/")
    ]
    if unexpected or not ALLOWED_ROOT.issubset(names):
        raise SystemExit(f"user package allowlist mismatch: {unexpected}")
    if "browser/chromium/chrome.exe" not in names and "runtime/browser/chromium/chrome.exe" not in names:
        raise SystemExit("bundled Chromium executable missing")
    for binary in ("runtime/ffmpeg/bin/ffmpeg.exe", "runtime/ffmpeg/bin/ffprobe.exe"):
        if binary not in names:
            raise SystemExit(f"bundled media runtime missing: {binary}")
    forbidden = [name for name in names if not name.startswith("runtime/postflow/") and any(mark in name.lower() for mark in FORBIDDEN_MARKERS)]
    if forbidden:
        raise SystemExit(f"forbidden user package entries: {forbidden}")
    print(f"PACKAGE_ALLOWLIST_OK {len(names)} files; bundled Chromium present; UTF-8 names preserved")


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("stage")
    create.add_argument("target")
    verify = subparsers.add_parser("verify")
    verify.add_argument("target")
    args = parser.parse_args()
    if args.command == "create":
        create_archive(args.stage, args.target)
    else:
        verify_archive(args.target)


if __name__ == "__main__":
    main()
