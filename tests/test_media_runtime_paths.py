from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import runtime_paths


def _binary_name(kind: str) -> str:
    return kind + (".exe" if runtime_paths.os.name == "nt" else "")


def _make_bundle(root: Path) -> tuple[Path, Path]:
    bin_dir = root / "runtime" / "ffmpeg" / "bin"
    bin_dir.mkdir(parents=True)
    ffmpeg = bin_dir / _binary_name("ffmpeg")
    ffprobe = bin_dir / _binary_name("ffprobe")
    ffmpeg.write_bytes(b"test")
    ffprobe.write_bytes(b"test")
    return ffmpeg, ffprobe


def test_release_resolves_bundled_binaries_beside_executable(tmp_path):
    ffmpeg, ffprobe = _make_bundle(tmp_path)
    with patch.object(runtime_paths, "FROZEN", True), \
         patch.object(runtime_paths, "EXECUTABLE_DIR", tmp_path), \
         patch.object(runtime_paths, "RESOURCE_DIR", tmp_path / "_internal"), \
         patch.object(runtime_paths.shutil, "which", return_value=None):
        assert Path(runtime_paths.get_ffmpeg_path()) == ffmpeg.resolve()
        assert Path(runtime_paths.get_ffprobe_path()) == ffprobe.resolve()


def test_release_ignores_stale_persisted_absolute_path(tmp_path):
    ffmpeg, _ = _make_bundle(tmp_path)
    stale = r"D:\Video\videomix\resources\backend\ffmpeg.exe"
    with patch.object(runtime_paths, "FROZEN", True), \
         patch.object(runtime_paths, "EXECUTABLE_DIR", tmp_path), \
         patch.object(runtime_paths, "RESOURCE_DIR", tmp_path / "_internal"):
        assert Path(runtime_paths.get_ffmpeg_path(stale)) == ffmpeg.resolve()


def test_release_move_to_another_directory_keeps_relative_resolution(tmp_path):
    first = tmp_path / "C-drive" / "MixcutAI"
    second = tmp_path / "D-drive" / "Test" / "MixcutAI"
    _make_bundle(first)
    moved_ffmpeg, moved_ffprobe = _make_bundle(second)
    with patch.object(runtime_paths, "FROZEN", True), \
         patch.object(runtime_paths, "EXECUTABLE_DIR", second), \
         patch.object(runtime_paths, "RESOURCE_DIR", second / "_internal"):
        assert Path(runtime_paths.get_ffmpeg_path()) == moved_ffmpeg.resolve()
        assert Path(runtime_paths.get_ffprobe_path()) == moved_ffprobe.resolve()


@pytest.mark.parametrize("kind", ["ffmpeg", "ffprobe"])
def test_release_missing_component_never_falls_back_to_path(tmp_path, kind):
    with patch.object(runtime_paths, "FROZEN", True), \
         patch.object(runtime_paths, "EXECUTABLE_DIR", tmp_path), \
         patch.object(runtime_paths, "RESOURCE_DIR", tmp_path / "_internal"), \
         patch.object(runtime_paths.shutil, "which") as which:
        with pytest.raises(runtime_paths.MediaRuntimeError, match="重新安装"):
            runtime_paths._resolve_media_binary(kind)
        which.assert_not_called()


def test_source_mode_may_fall_back_to_path(tmp_path):
    system_ffmpeg = tmp_path / _binary_name("ffmpeg")
    system_ffmpeg.write_bytes(b"test")
    with patch.object(runtime_paths, "FROZEN", False), \
         patch.object(runtime_paths, "SOURCE_DIR", tmp_path / "project"), \
         patch.object(runtime_paths, "EXECUTABLE_DIR", tmp_path / "project"), \
         patch.object(runtime_paths, "RESOURCE_DIR", tmp_path / "project"), \
         patch.object(runtime_paths.shutil, "which", return_value=str(system_ffmpeg)):
        assert Path(runtime_paths.get_ffmpeg_path()) == system_ffmpeg.resolve()


def test_runtime_status_validates_versions_without_exposing_paths(tmp_path):
    _make_bundle(tmp_path)
    completed = subprocess.CompletedProcess([], 0, stdout="ffmpeg version 8.1.2 test\n", stderr="")
    with patch.object(runtime_paths, "FROZEN", True), \
         patch.object(runtime_paths, "EXECUTABLE_DIR", tmp_path), \
         patch.object(runtime_paths, "RESOURCE_DIR", tmp_path / "_internal"), \
         patch.object(runtime_paths.subprocess, "run", return_value=completed):
        status = runtime_paths.get_media_runtime_status(refresh=True)
    assert status["ready"] is True
    assert status["ffmpeg"]["version"] == "8.1.2"
    assert status["ffprobe"]["version"] == "8.1.2"
    assert "path" not in status["ffmpeg"]
    assert "path" not in status["ffprobe"]


def test_public_settings_hide_legacy_media_paths():
    import commercial_api

    with patch.object(
        commercial_api,
        "settings",
        return_value={"ffmpeg_path": r"D:\old\ffmpeg.exe", "ffprobe_path": r"D:\old\ffprobe.exe", "output_dir": "E:\\Output"},
    ):
        value = commercial_api.public_settings()
    assert value == {"output_dir": "E:\\Output"}


def test_media_consumers_delegate_to_unified_resolver(tmp_path):
    binary = str(tmp_path / _binary_name("ffmpeg"))
    Path(binary).write_bytes(b"test")
    import video_engine
    import video_scan

    with patch.object(video_engine, "get_ffmpeg_path", return_value=binary) as engine_resolver:
        assert video_engine.find_ffmpeg(r"D:\stale\ffmpeg.exe") == binary
        engine_resolver.assert_called_once_with()
    with patch.object(video_scan, "get_ffmpeg_path", return_value=binary) as scan_resolver:
        assert video_scan.find_executable("ffmpeg", r"D:\stale\ffmpeg.exe") == binary
        scan_resolver.assert_called_once_with()


def test_settings_ui_has_no_media_path_inputs():
    source = (Path(__file__).parents[1] / "ui-commercial" / "src" / "main.jsx").read_text(encoding="utf-8")
    start = source.index("function SettingsPage(")
    end = source.index("\nfunction PublishPage", start)
    settings_component = source[start:end]
    assert "FFmpeg 路径" not in settings_component
    assert "FFprobe 路径" not in settings_component
    assert "视频处理引擎" in settings_component
