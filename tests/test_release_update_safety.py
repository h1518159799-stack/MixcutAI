from __future__ import annotations

import hashlib
import json
import stat
import sys
from pathlib import Path

import pytest

import update_service
import updater_main
from shared.update_manifest import UpdateArtifact, UpdateManifest


def make_manifest(*, version="9.9.9", payload_sha="a" * 64, file_size=123, updater=None):
    return UpdateManifest(
        version=version,
        download_url="https://updates.example/new.exe",
        sha256=payload_sha,
        file_size=file_size,
        release_notes="",
        updater_version=version,
        updater_artifact=updater,
    )


def test_production_domain_defaults_and_version():
    root = Path(__file__).parents[1]
    config = json.loads((root / "default_config.json").read_text(encoding="utf-8"))
    assert config["app_version"] == "1.3.27"
    assert config["license_server_url"] == "https://license.ghwc.cn"
    assert config["update_manifest_url"] == "https://update.ghwc.cn/update.json"
    assert update_service.current_version() == "1.3.27"


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
    manifest = make_manifest(payload_sha=digest, file_size=len(payload))
    monkeypatch.setattr(update_service, "UPDATE_DIR", tmp_path)
    monkeypatch.setattr(update_service, "_open_https", lambda *_args, **_kwargs: _Response(payload))
    progress = []
    target = update_service.download_update(manifest, progress_callback=lambda done, total: progress.append((done, total)))
    assert target.read_bytes() == payload
    assert progress
    assert progress[-1] == (len(payload), len(payload))


def test_download_update_removes_partial_on_hash_mismatch(tmp_path, monkeypatch):
    payload = b"broken update"
    manifest = make_manifest(version="9.9.8", payload_sha="0" * 64, file_size=len(payload))
    monkeypatch.setattr(update_service, "UPDATE_DIR", tmp_path)
    monkeypatch.setattr(update_service, "_open_https", lambda *_args, **_kwargs: _Response(payload))
    with pytest.raises(update_service.UpdateError, match="SHA256"):
        update_service.download_update(manifest)
    assert not list(tmp_path.glob("*.part"))


def test_manifest_requires_and_exposes_size_bytes(monkeypatch):
    payload = {
        "version": "9.9.9",
        "title": "test",
        "content": "",
        "download_url": "https://updates.example/new.exe",
        "sha256": "a" * 64,
        "size_bytes": 123,
        "force_update": False,
    }
    raw = json.dumps(payload).encode("utf-8")
    monkeypatch.setattr(update_service, "_open_https", lambda *_args, **_kwargs: _Response(raw))
    manifest = update_service.fetch_update_manifest("https://updates.example/update.json")
    assert manifest.size_bytes == 123
    assert manifest.public_dict()["file_size"] == 123


def test_download_update_rejects_manifest_size_mismatch(tmp_path, monkeypatch):
    payload = b"MZ-wrong-size"
    manifest = make_manifest(version="9.9.7", payload_sha=hashlib.sha256(payload).hexdigest(), file_size=len(payload) + 1)
    monkeypatch.setattr(update_service, "UPDATE_DIR", tmp_path)
    monkeypatch.setattr(update_service, "_open_https", lambda *_args, **_kwargs: _Response(payload))
    with pytest.raises(update_service.UpdateError, match="update.json"):
        update_service.download_update(manifest)
    assert not list(tmp_path.glob("*.part"))


def test_manifest_accepts_optional_updater_metadata(monkeypatch):
    updater_payload = {
        "url": "https://updates.example/MixcutUpdater.exe",
        "sha256": "b" * 64,
        "size_bytes": 1234,
    }
    payload = {
        "version": "9.9.9",
        "title": "test",
        "content": "",
        "download_url": "https://updates.example/new.exe",
        "sha256": "a" * 64,
        "size_bytes": 123,
        "force_update": False,
        "updater": updater_payload,
    }
    raw = json.dumps(payload).encode("utf-8")
    monkeypatch.setattr(update_service, "_open_https", lambda *_args, **_kwargs: _Response(raw))
    manifest = update_service.fetch_update_manifest("https://updates.example/update.json")
    assert manifest.updater_url == updater_payload["url"]
    assert manifest.updater_sha256 == updater_payload["sha256"]
    assert manifest.updater_size_bytes == updater_payload["size_bytes"]


def test_manifest_from_dict_ignores_unknown_fields_and_nested_updater():
    manifest = update_service.UpdateManifest.from_dict({
        "version": "9.9.9",
        "title": "test",
        "content": "",
        "download_url": "https://updates.example/new.exe",
        "sha256": "a" * 64,
        "size_bytes": 123,
        "force_update": False,
        "updater": {
            "url": "https://updates.example/MixcutUpdater.exe",
            "sha256": "b" * 64,
            "size_bytes": 456,
        },
        "future_field": "ignored",
    })
    assert manifest.updater_url == "https://updates.example/MixcutUpdater.exe"
    assert manifest.updater_size_bytes == 456


def test_shared_manifest_round_trip_never_passes_nested_updater_to_constructor():
    original = make_manifest(
        updater=UpdateArtifact("https://updates.example/MixcutUpdater.exe", "b" * 64, 456)
    )
    payload = original.public_dict()
    assert "updater" in payload
    restored = UpdateManifest.from_dict(payload)
    assert restored.version == original.version
    assert restored.updater_sha256 == "b" * 64


def test_incomplete_updater_metadata_disables_recovery(monkeypatch):
    payload = {
        "version": "9.9.9",
        "title": "test",
        "content": "",
        "download_url": "https://updates.example/new.exe",
        "sha256": "a" * 64,
        "size_bytes": 123,
        "force_update": False,
        "updater": {"url": "https://updates.example/MixcutUpdater.exe"},
    }
    raw = json.dumps(payload).encode("utf-8")
    monkeypatch.setattr(update_service, "_open_https", lambda *_args, **_kwargs: _Response(raw))
    manifest = update_service.fetch_update_manifest("https://updates.example/update.json")
    assert manifest.updater_url is None
    assert manifest.updater_sha256 is None
    assert manifest.updater_size_bytes is None


@pytest.mark.parametrize("failure", [ValueError("bad json"), OSError("offline")])
def test_startup_update_flow_never_propagates_update_failures(monkeypatch, failure):
    monkeypatch.setattr(update_service, "_startup_update_flow", lambda _config: (_ for _ in ()).throw(failure))
    assert update_service.startup_update_flow({"auto_check_update": "1"}) == "continue"


def test_updater_path_does_not_depend_on_cwd(monkeypatch, tmp_path):
    updater = tmp_path / "MixcutUpdater.exe"
    updater.write_bytes(b"updater")
    monkeypatch.setattr(update_service, "SOURCE_DIR", tmp_path / "source")
    monkeypatch.setattr(update_service.sys, "frozen", True, raising=False)
    monkeypatch.setattr(update_service.sys, "executable", str(tmp_path / "MixcutAI.exe"), raising=False)
    assert update_service.get_updater_path() == updater


def test_missing_updater_without_manifest_metadata_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setattr(update_service, "get_updater_path", lambda: tmp_path / "MixcutUpdater.exe")
    manifest = make_manifest()
    with pytest.raises(update_service.UpdateError, match="更新组件缺失"):
        update_service.ensure_updater(manifest)


def test_missing_updater_is_restored_with_verified_metadata(tmp_path, monkeypatch):
    payload = b"verified-updater"
    digest = hashlib.sha256(payload).hexdigest()
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    updater_path = app_dir / "MixcutUpdater.exe"
    download_dir = tmp_path / "updates"
    monkeypatch.setattr(update_service, "get_updater_path", lambda: updater_path)
    monkeypatch.setattr(update_service, "UPDATE_DIR", download_dir)
    monkeypatch.setattr(update_service, "_open_https", lambda *_args, **_kwargs: _Response(payload, "https://updates.example/MixcutUpdater.exe"))
    manifest = make_manifest(updater=UpdateArtifact(
        "https://updates.example/MixcutUpdater.exe", digest, len(payload)
    ))
    assert update_service.ensure_updater(manifest) == updater_path
    assert updater_path.read_bytes() == payload


def test_existing_stale_updater_is_replaced_before_launch(tmp_path, monkeypatch):
    payload = b"updater-v1.3.17"
    digest = hashlib.sha256(payload).hexdigest()
    updater_path = tmp_path / "MixcutUpdater.exe"
    updater_path.write_bytes(b"updater-v1.3.16")
    monkeypatch.setattr(update_service, "get_updater_path", lambda: updater_path)
    monkeypatch.setattr(
        update_service,
        "_open_https",
        lambda *_args, **_kwargs: _Response(payload, "https://updates.example/MixcutUpdater.exe"),
    )
    manifest = make_manifest(
        version="1.3.17",
        updater=UpdateArtifact("https://updates.example/MixcutUpdater.exe", digest, len(payload)),
    )

    assert update_service.ensure_updater(manifest) == updater_path
    assert updater_path.read_bytes() == payload


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


def test_rollback_keeps_intact_original_when_initial_replace_is_denied(tmp_path, monkeypatch):
    current = tmp_path / "MixcutAI.exe"
    new = tmp_path / "MixcutAI-new.exe"
    log = tmp_path / "update.log"
    current.write_bytes(b"old")
    new.write_bytes(b"new")
    digest = hashlib.sha256(b"new").hexdigest()
    launches = []
    original_replace = updater_main.os.replace

    def deny_incoming(source, target):
        if Path(source).suffix == ".new" and Path(target) == current:
            raise PermissionError(5, "denied", str(target))
        return original_replace(source, target)

    monkeypatch.setattr(updater_main.os, "replace", deny_incoming)
    monkeypatch.setattr(updater_main.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(updater_main, "launch", lambda path: launches.append(path) or _RunningProcess())

    with pytest.raises(PermissionError):
        updater_main.replace_with_rollback(current, new, digest, log)
    assert current.read_bytes() == b"old"
    assert launches == [current]
    assert "rollback skipped: original executable is still intact" in log.read_text(encoding="utf-8")


def test_locked_executable_is_retried_until_released(tmp_path, monkeypatch):
    current = tmp_path / "MixcutAI.exe"
    new = tmp_path / "MixcutAI-new.exe"
    log = tmp_path / "update.log"
    current.write_bytes(b"old")
    new.write_bytes(b"new")
    digest = hashlib.sha256(b"new").hexdigest()
    attempts = {"count": 0}
    original_replace = updater_main.os.replace

    def release_after_three_attempts(source, target):
        if Path(source).suffix == ".new" and Path(target) == current and attempts["count"] < 3:
            attempts["count"] += 1
            raise PermissionError(5, "locked", str(target))
        return original_replace(source, target)

    monkeypatch.setattr(updater_main.os, "replace", release_after_three_attempts)
    monkeypatch.setattr(updater_main.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(updater_main, "launch", lambda _path: _RunningProcess())

    assert updater_main.replace_with_rollback(current, new, digest, log) == 0
    assert attempts["count"] == 3
    assert current.read_bytes() == b"new"


def test_read_only_current_executable_can_be_replaced(tmp_path, monkeypatch):
    current = tmp_path / "MixcutAI.exe"
    new = tmp_path / "MixcutAI-new.exe"
    log = tmp_path / "update.log"
    current.write_bytes(b"old")
    current.chmod(stat.S_IREAD)
    new.write_bytes(b"new")
    digest = hashlib.sha256(b"new").hexdigest()
    monkeypatch.setattr(updater_main, "launch", lambda _path: _RunningProcess())

    assert updater_main.replace_with_rollback(current, new, digest, log) == 0
    assert current.read_bytes() == b"new"


def test_updater_rejects_wrong_expected_size(tmp_path, monkeypatch):
    current = tmp_path / "MixcutAI.exe"
    new = tmp_path / "MixcutAI-new.exe"
    log = tmp_path / "update.log"
    current.write_bytes(b"old")
    new.write_bytes(b"new")
    digest = hashlib.sha256(b"new").hexdigest()
    with pytest.raises(RuntimeError, match="大小"):
        updater_main.replace_with_rollback(current, new, digest, log, expected_size=4)
    assert current.read_bytes() == b"old"


def test_updater_main_consumes_shared_manifest_snapshot(tmp_path, monkeypatch):
    current = tmp_path / "MixcutAI.exe"
    new = tmp_path / "MixcutAI-new.exe"
    backup = tmp_path / "mixcut.db"
    log_path = tmp_path / "update.log"
    manifest_path = tmp_path / "update.json"
    current.write_bytes(b"old")
    new.write_bytes(b"new")
    backup.write_bytes(b"db")
    manifest = make_manifest(
        version=updater_main.UPDATER_VERSION,
        payload_sha=hashlib.sha256(b"new").hexdigest(),
        file_size=3,
    )
    manifest_path.write_text(manifest.to_json(), encoding="utf-8")
    captured = {}
    monkeypatch.setattr(updater_main, "wait_for_process", lambda *_args: True)
    monkeypatch.setattr(
        updater_main,
        "replace_with_rollback",
        lambda _current, _new, sha, _log, size: captured.update(sha=sha, size=size) or 0,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "MixcutUpdater.exe", "--pid", "123", "--current", str(current),
            "--new", str(new), "--manifest", str(manifest_path),
            "--db-backup", str(backup), "--log", str(log_path),
        ],
    )
    assert updater_main.main() == 0
    assert captured == {"sha": manifest.sha256, "size": manifest.file_size}


def test_v1316_updater_accepts_v1318_bridge_manifest(tmp_path, monkeypatch):
    current = tmp_path / "MixcutAI.exe"
    new = tmp_path / "MixcutAI-new.exe"
    backup = tmp_path / "mixcut.db"
    log_path = tmp_path / "update.log"
    manifest_path = tmp_path / "update.json"
    current.write_bytes(b"old-1.3.16")
    new.write_bytes(b"new-1.3.18")
    backup.write_bytes(b"db")
    manifest = UpdateManifest(
        version="1.3.18",
        download_url="https://updates.example/MixcutAI-v1.3.18.exe",
        sha256=hashlib.sha256(new.read_bytes()).hexdigest(),
        file_size=new.stat().st_size,
        release_notes="bridge",
        updater_version="1.3.16",
    )
    manifest_path.write_text(manifest.to_json(), encoding="utf-8")
    captured = {}
    monkeypatch.setattr(updater_main, "UPDATER_VERSION", "1.3.16")
    monkeypatch.setattr(updater_main, "wait_for_process", lambda *_args: True)
    monkeypatch.setattr(
        updater_main,
        "replace_with_rollback",
        lambda _current, _new, sha, _log, size: captured.update(sha=sha, size=size) or 0,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "MixcutUpdater.exe", "--pid", "123", "--current", str(current),
            "--new", str(new), "--manifest", str(manifest_path),
            "--db-backup", str(backup), "--log", str(log_path),
        ],
    )

    assert updater_main.main() == 0
    assert captured == {"sha": manifest.sha256, "size": manifest.file_size}


def test_executable_update_preserves_bundled_media_runtime(tmp_path, monkeypatch):
    current = tmp_path / "MixcutAI.exe"
    new = tmp_path / "MixcutAI-new.exe"
    log = tmp_path / "update.log"
    media_dir = tmp_path / "runtime" / "ffmpeg" / "bin"
    media_dir.mkdir(parents=True)
    ffmpeg = media_dir / "ffmpeg.exe"
    ffprobe = media_dir / "ffprobe.exe"
    ffmpeg.write_bytes(b"bundled-ffmpeg")
    ffprobe.write_bytes(b"bundled-ffprobe")
    current.write_bytes(b"old")
    new.write_bytes(b"new")
    digest = hashlib.sha256(b"new").hexdigest()
    monkeypatch.setattr(updater_main, "launch", lambda _path: _RunningProcess())
    monkeypatch.setattr(updater_main.time, "sleep", lambda _seconds: None)

    assert updater_main.replace_with_rollback(current, new, digest, log) == 0
    assert ffmpeg.read_bytes() == b"bundled-ffmpeg"
    assert ffprobe.read_bytes() == b"bundled-ffprobe"


def test_updater_restores_old_executable_when_new_version_fails(tmp_path, monkeypatch):
    current = tmp_path / "MixcutAI.exe"
    new = tmp_path / "MixcutAI-new.exe"
    log = tmp_path / "update.log"
    current.write_bytes(b"old")
    new.write_bytes(b"bad-new")
    digest = hashlib.sha256(b"bad-new").hexdigest()
    launches = []
    monkeypatch.setattr(updater_main, "launch", lambda _path: launches.append(_path) or _RunningProcess())
    monkeypatch.setattr(updater_main.time, "sleep", lambda _seconds: None)
    assert updater_main.replace_with_rollback(current, new, digest, log) == 0
    assert current.read_bytes() == b"bad-new"
    assert launches == [current]
