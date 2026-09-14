"""Verified self-repair for the bundled FFmpeg runtime."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import runtime_paths


RUNTIME_MANIFEST_URL = "https://update.ghwc.cn/runtime/runtime_manifest.json"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_RUNTIME_BYTES = 700 * 1024 * 1024


class RuntimeRepairError(RuntimeError):
    pass


def runtime_needs_repair(status: dict, manifest_url: str = RUNTIME_MANIFEST_URL) -> bool:
    if not status.get("ready"):
        return True
    local_manifest = runtime_paths.EXECUTABLE_DIR / "runtime" / "ffmpeg" / "runtime_manifest.json"
    try:
        local = json.loads(local_manifest.read_text(encoding="utf-8-sig")) if local_manifest.is_file() else {}
        remote = fetch_runtime_manifest(manifest_url)
        return str(local.get("ffmpeg_version") or "") != str(remote["ffmpeg_version"])
    except (OSError, ValueError, json.JSONDecodeError, RuntimeRepairError):
        # A healthy local runtime must remain usable while the repair server is offline.
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def _open_https(url: str, timeout: int = 60):
    parsed = urlparse(str(url or ""))
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise RuntimeRepairError("视频组件修复地址必须使用HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": "MixcutAI-RuntimeRepair/1"})
    response = urllib.request.urlopen(request, timeout=timeout)
    if urlparse(response.geturl()).scheme.lower() != "https":
        response.close()
        raise RuntimeRepairError("视频组件下载被重定向到非HTTPS地址")
    return response


def fetch_runtime_manifest(url: str = RUNTIME_MANIFEST_URL) -> dict:
    try:
        with _open_https(url, 15) as response:
            raw = response.read(MAX_MANIFEST_BYTES + 1)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        raise RuntimeRepairError(f"无法连接视频组件修复服务器：{exc}") from exc
    if len(raw) > MAX_MANIFEST_BYTES:
        raise RuntimeRepairError("视频组件清单文件过大")
    try:
        manifest = json.loads(raw.decode("utf-8-sig"))
        size = int(manifest["size_bytes"])
        sha = str(manifest["sha256"]).lower()
        files = manifest["files"]
        version = str(manifest["ffmpeg_version"])
        url = str(manifest["url"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeRepairError("视频组件清单格式无效") from exc
    if size <= 0 or size > MAX_RUNTIME_BYTES or len(sha) != 64:
        raise RuntimeRepairError("视频组件清单大小或SHA256无效")
    if not isinstance(files, dict) or not all(name in files for name in ("bin/ffmpeg.exe", "bin/ffprobe.exe")):
        raise RuntimeRepairError("视频组件清单缺少文件校验信息")
    if urlparse(url).scheme.lower() != "https" or not version:
        raise RuntimeRepairError("视频组件版本或下载地址无效")
    return manifest


def _download_runtime(manifest: dict, destination: Path) -> None:
    expected_size = int(manifest["size_bytes"])
    try:
        with _open_https(str(manifest["url"]), 60) as response, destination.open("wb") as output:
            header_size = int(response.headers.get("Content-Length") or 0)
            if header_size and header_size != expected_size:
                raise RuntimeRepairError("视频组件下载大小与清单不一致")
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_RUNTIME_BYTES:
                    raise RuntimeRepairError("视频组件修复包超过大小限制")
                output.write(chunk)
            output.flush(); os.fsync(output.fileno())
    except RuntimeRepairError:
        raise
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        raise RuntimeRepairError(f"视频组件下载失败：{exc}") from exc
    if destination.stat().st_size != expected_size or _sha256(destination) != str(manifest["sha256"]).lower():
        raise RuntimeRepairError("视频组件修复包SHA256或大小校验失败")


def _safe_extract(archive: Path, staging: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            relative = PurePosixPath(member.filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeRepairError("视频组件修复包包含不安全路径")
            target = staging.joinpath(*relative.parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def repair_media_runtime(manifest_url: str = RUNTIME_MANIFEST_URL) -> dict:
    manifest = fetch_runtime_manifest(manifest_url)
    parent = runtime_paths.EXECUTABLE_DIR
    target = parent / "runtime" / "ffmpeg"
    try:
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".mixcut-runtime-", dir=parent) as temporary:
            work = Path(temporary)
            archive = work / "runtime.zip"
            staging = work / "ffmpeg"
            staging.mkdir()
            _download_runtime(manifest, archive)
            _safe_extract(archive, staging)
            for relative, expected in manifest["files"].items():
                path = staging.joinpath(*PurePosixPath(relative).parts)
                if not path.is_file() or _sha256(path) != str(expected).lower():
                    raise RuntimeRepairError(f"视频组件文件损坏：{relative}")
            (staging / "runtime_manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            backup = parent / "runtime" / "ffmpeg.backup"
            backup.parent.mkdir(parents=True, exist_ok=True)
            if backup.exists(): shutil.rmtree(backup)
            if target.exists(): os.replace(target, backup)
            try:
                os.replace(staging, target)
            except Exception:
                if backup.exists() and not target.exists(): os.replace(backup, target)
                raise
            if backup.exists(): shutil.rmtree(backup)
    except PermissionError as exc:
        raise RuntimeRepairError(f"当前软件目录无写入权限：{parent}；{exc}") from exc
    except OSError as exc:
        raise RuntimeRepairError(f"视频组件安装失败：{exc}") from exc
    runtime_paths.clear_media_runtime_cache()
    status = runtime_paths.get_media_runtime_status(refresh=True, include_paths=True)
    if not status["ready"]:
        raise RuntimeRepairError(status["message"] + " " + status["ffmpeg"]["error"] + " " + status["ffprobe"]["error"])
    return status
