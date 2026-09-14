from __future__ import annotations

import hashlib
import io
import json
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

import runtime_paths
import runtime_repair_service as repair


class Response(io.BytesIO):
    def __init__(self, data: bytes, url: str):
        super().__init__(data); self.url = url
        self.headers = {"Content-Length": str(len(data))}
    def geturl(self): return self.url
    def __enter__(self): return self
    def __exit__(self, *_args): self.close()


def package():
    files = {"bin/ffmpeg.exe": b"ffmpeg-new", "bin/ffprobe.exe": b"ffprobe-new"}
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as bundle:
        for name, data in files.items(): bundle.writestr(name, data)
    archive = stream.getvalue()
    manifest = {
        "ffmpeg_version": "8.1.2", "url": "https://update.example/runtime.zip",
        "sha256": hashlib.sha256(archive).hexdigest(), "size_bytes": len(archive),
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
    }
    return manifest, archive, files


@pytest.mark.parametrize("initial", ["missing_ffmpeg", "missing_runtime", "corrupt"])
def test_runtime_missing_or_corrupt_is_restored(tmp_path, monkeypatch, initial):
    target = tmp_path / "runtime" / "ffmpeg" / "bin"
    if initial != "missing_runtime":
        target.mkdir(parents=True)
        (target / "ffprobe.exe").write_bytes(b"old")
        if initial == "corrupt": (target / "ffmpeg.exe").write_bytes(b"corrupt")
    manifest, archive, files = package()
    manifest_raw = json.dumps(manifest).encode()
    def opener(url, _timeout=60):
        data = manifest_raw if "manifest" in url else archive
        return Response(data, url)
    monkeypatch.setattr(repair, "_open_https", opener)
    monkeypatch.setattr(runtime_paths, "EXECUTABLE_DIR", tmp_path)
    monkeypatch.setattr(runtime_paths, "clear_media_runtime_cache", lambda: None)
    monkeypatch.setattr(runtime_paths, "get_media_runtime_status", lambda **_kw: {
        "ready": True, "message": "ready", "ffmpeg": {"error": ""}, "ffprobe": {"error": ""}})
    result = repair.repair_media_runtime("https://update.example/runtime_manifest.json")
    assert result["ready"] is True
    for name, data in files.items():
        assert (tmp_path / "runtime" / "ffmpeg" / name).read_bytes() == data
    assert (tmp_path / "runtime" / "ffmpeg" / "runtime_manifest.json").is_file()


def test_runtime_repair_network_failure_is_actionable(monkeypatch):
    monkeypatch.setattr(repair, "_open_https", lambda *_a, **_k: (_ for _ in ()).throw(urllib.error.URLError("offline")))
    with pytest.raises(repair.RuntimeRepairError, match="无法连接视频组件修复服务器"):
        repair.repair_media_runtime("https://update.example/runtime_manifest.json")


def test_runtime_archive_hash_mismatch_is_rejected(tmp_path, monkeypatch):
    manifest, archive, _files = package()
    manifest["sha256"] = "0" * 64
    raw = json.dumps(manifest).encode()
    monkeypatch.setattr(repair, "_open_https", lambda url, _timeout=60: Response(raw if "manifest" in url else archive, url))
    monkeypatch.setattr(runtime_paths, "EXECUTABLE_DIR", tmp_path)
    with pytest.raises(repair.RuntimeRepairError, match="SHA256"):
        repair.repair_media_runtime("https://update.example/runtime_manifest.json")


def test_older_local_runtime_requests_upgrade(tmp_path, monkeypatch):
    local = tmp_path / "runtime" / "ffmpeg" / "runtime_manifest.json"
    local.parent.mkdir(parents=True)
    local.write_text(json.dumps({"ffmpeg_version": "7.0"}), encoding="utf-8")
    monkeypatch.setattr(runtime_paths, "EXECUTABLE_DIR", tmp_path)
    monkeypatch.setattr(repair, "fetch_runtime_manifest", lambda _url: {"ffmpeg_version": "8.1.2"})
    assert repair.runtime_needs_repair({"ready": True}, "https://update.example/runtime_manifest.json") is True


def test_healthy_runtime_stays_available_when_repair_server_is_offline(tmp_path, monkeypatch):
    local = tmp_path / "runtime" / "ffmpeg" / "runtime_manifest.json"
    local.parent.mkdir(parents=True)
    local.write_text(json.dumps({"ffmpeg_version": "8.1.2"}), encoding="utf-8")
    monkeypatch.setattr(runtime_paths, "EXECUTABLE_DIR", tmp_path)
    monkeypatch.setattr(repair, "fetch_runtime_manifest", lambda _url: (_ for _ in ()).throw(repair.RuntimeRepairError("offline")))
    assert repair.runtime_needs_repair({"ready": True}, "https://update.example/runtime_manifest.json") is False
