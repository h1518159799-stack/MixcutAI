from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import update_service
import updater_main


class _Headers(dict):
    pass


class _Response:
    def __init__(self, payload: bytes, url: str = "https://updates.example/new.exe"):
        self.payload = payload
        self.offset = 0
        self.headers = _Headers({"Content-Length": str(len(payload))})
        self.url = url

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.payload) - self.offset
        value = self.payload[self.offset : self.offset + size]
        self.offset += len(value)
        return value

    def geturl(self) -> str:
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_download_update_reports_real_progress_and_checks_sha(tmp_path, monkeypatch):
    payload = b"MZ" + (b"release-bytes" * 1000)
    digest = hashlib.sha256(payload).hexdigest()
    manifest = update_service.UpdateManifest(
        version="9.9.9",
        title="测试更新",
        content="",
        download_url="https://updates.example/new.exe",
        sha256=digest,
        force_update=False,
    )
    monkeypatch.setattr(update_service, "UPDATE_DIR", tmp_path)
    monkeypatch.setattr(update_service, "_open_https", lambda *_args, **_kwargs: _Response(payload))
    progress = []
    target = update_service.download_update(manifest, progress_callback=lambda done, total: progress.append((done, total)))
    assert target.read_bytes() == payload
    assert progress
    assert progress[-1] == (len(payload), len(payload))


def test_download_update_removes_partial_on_hash_mismatch(tmp_path, monkeypatch):
    payload = b"broken update"
    manifest = update_service.UpdateManifest(
        version="9.9.8",
        title="测试更新",
        content="",
        download_url="https://updates.example/new.exe",
        sha256="0" * 64,
        force_update=False,
    )
    monkeypatch.setattr(update_service, "UPDATE_DIR", tmp_path)
    monkeypatch.setattr(update_service, "_open_https", lambda *_args, **_kwargs: _Response(payload))
    with pytest.raises(update_service.UpdateError, match="SHA256"):
        update_service.download_update(manifest)
    assert not list(tmp_path.glob("*.part"))


class _RunningProcess:
    pid = 12345

    @staticmethod
    def poll():
        return None


class _FailedProcess:
    pid = 12346
    returncode = 2

    @staticmethod
    def poll():
        return 2


def test_updater_replaces_verified_executable(tmp_path, monkeypatch):
    current = tmp_path / "MixcutAI.exe"
    new = tmp_path / "MixcutAI-new.exe"
    log = tmp_path / "update.log"
    current.write_bytes(b"old")
    new.write_bytes(b"new")
    digest = hashlib.sha256(b"new").hexdigest()
    monkeypatch.setattr(updater_main, "launch", lambda _path: _RunningProcess())
    monkeypatch.setattr(updater_main.time, "sleep", lambda _seconds: None)
    assert updater_main.replace_with_rollback(current, new, digest, log) == 0
    assert current.read_bytes() == b"new"
    assert current.with_suffix(".exe.bak").read_bytes() == b"old"


def test_updater_restores_old_executable_when_new_version_fails(tmp_path, monkeypatch):
    current = tmp_path / "MixcutAI.exe"
    new = tmp_path / "MixcutAI-new.exe"
    log = tmp_path / "update.log"
    current.write_bytes(b"old")
    new.write_bytes(b"bad-new")
    digest = hashlib.sha256(b"bad-new").hexdigest()
    launches = iter([_FailedProcess(), _RunningProcess()])
    monkeypatch.setattr(updater_main, "launch", lambda _path: next(launches))
    monkeypatch.setattr(updater_main.time, "sleep", lambda _seconds: None)
    with pytest.raises(RuntimeError, match="异常退出"):
        updater_main.replace_with_rollback(current, new, digest, log)
    assert current.read_bytes() == b"old"
