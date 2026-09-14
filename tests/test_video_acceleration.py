import video_engine
import time
from timeline_engine import video_codec_args


def test_auto_prefers_nvenc_when_ffmpeg_reports_it(monkeypatch):
    monkeypatch.setattr(video_engine, "detect_video_encoders", lambda _ffmpeg: ["h264_nvenc"])
    monkeypatch.setattr(video_engine, "probe_video_encoder_result", lambda _ffmpeg, _encoder: (True, ""))
    assert video_engine.select_video_encoder("ffmpeg.exe", "auto") == ("h264_nvenc", ["h264_nvenc"])


def test_auto_and_nvenc_fallback_to_cpu_without_gpu_encoder(monkeypatch):
    monkeypatch.setattr(video_engine, "detect_video_encoders", lambda _ffmpeg: [])
    assert video_engine.select_video_encoder("ffmpeg.exe", "auto")[0] == "libx264"
    assert video_engine.select_video_encoder("ffmpeg.exe", "nvenc")[0] == "libx264"
    assert video_engine.select_video_encoder("ffmpeg.exe", "cpu")[0] == "libx264"


def test_advertised_but_unusable_nvenc_falls_back_to_cpu(monkeypatch):
    monkeypatch.setattr(video_engine, "detect_video_encoders", lambda _ffmpeg: ["h264_nvenc"])
    monkeypatch.setattr(video_engine, "probe_video_encoder_result", lambda _ffmpeg, _encoder: (False, "driver error"))
    assert video_engine.select_video_encoder("ffmpeg.exe", "auto")[0] == "libx264"


def test_codec_arguments_keep_legacy_x264_and_add_nvenc():
    assert "libx264" in video_codec_args("libx264")
    nvenc = video_codec_args("h264_nvenc")
    assert "h264_nvenc" in nvenc
    assert "-preset" in nvenc


def test_intel_qsv_and_amd_amf_are_supported(monkeypatch):
    monkeypatch.setattr(video_engine, "detect_gpu_info", lambda: {"name": "Intel UHD Graphics", "vendor": "Intel", "memory": "未知"})
    monkeypatch.setattr(video_engine, "detect_video_encoders", lambda _ffmpeg: ["h264_qsv"])
    monkeypatch.setattr(video_engine, "probe_video_encoder_result", lambda _ffmpeg, _encoder: (True, ""))
    assert video_engine.select_video_encoder("ffmpeg.exe", "auto")[0] == "h264_qsv"
    monkeypatch.setattr(video_engine, "detect_gpu_info", lambda: {"name": "AMD Radeon RX", "vendor": "AMD", "memory": "未知"})
    monkeypatch.setattr(video_engine, "detect_video_encoders", lambda _ffmpeg: ["h264_amf"])
    assert video_engine.select_video_encoder("ffmpeg.exe", "auto")[0] == "h264_amf"


def test_hardware_details_keep_failure_reason(monkeypatch):
    monkeypatch.setattr(video_engine, "detect_gpu_info", lambda: {"name": "NVIDIA GeForce RTX", "vendor": "NVIDIA", "memory": "8192 MB"})
    monkeypatch.setattr(video_engine, "detect_video_encoders", lambda _ffmpeg: ["h264_nvenc"])
    monkeypatch.setattr(video_engine, "probe_video_encoder_result", lambda _ffmpeg, _encoder: (False, "driver error"))
    details = video_engine.hardware_encoder_details("ffmpeg.exe")
    assert details["selected_encoder"] == "libx264"
    assert details["requested_encoder"] == "h264_nvenc"
    assert details["fallback_reason"] == "driver error"


def test_gpu_render_failure_retries_same_timeline_with_cpu(monkeypatch, tmp_path):
    calls = []

    def render(_ffmpeg, _timeline, output, _work_dir, encoder):
        calls.append(encoder)
        if encoder == "h264_nvenc":
            raise video_engine.VideoGenerationError("driver error")
        output.write_bytes(b"valid-mp4-placeholder")

    monkeypatch.setattr(video_engine, "render_timeline", render)
    actual, reason = video_engine.render_timeline_with_fallback(
        "ffmpeg.exe", {}, tmp_path / "out.mp4", tmp_path, "h264_nvenc", 1
    )
    assert calls == ["h264_nvenc", "libx264"]
    assert actual == "libx264"
    assert reason == "driver error"
    assert (tmp_path / "out.mp4").is_file()


def test_startup_gpu_detection_runs_only_once_and_is_non_blocking(monkeypatch):
    video_engine.reset_gpu_detection_for_tests()
    calls = []

    def detect():
        calls.append(True)
        time.sleep(0.05)
        return {"current_encoder": "libx264", "gpu_available": False}

    monkeypatch.setattr(video_engine, "_compute_video_acceleration_status", detect)
    started = time.perf_counter()
    for _ in range(10):
        video_engine.start_gpu_detection_once()
    assert time.perf_counter() - started < 0.04
    deadline = time.time() + 2
    while not calls and time.time() < deadline:
        time.sleep(0.01)
    assert len(calls) == 1
    video_engine.reset_gpu_detection_for_tests()


def test_gpu_detection_exception_becomes_safe_cpu_status(monkeypatch):
    monkeypatch.setattr(video_engine, "find_ffmpeg", lambda: (_ for _ in ()).throw(RuntimeError("ffmpeg probe error")))
    result = video_engine._compute_video_acceleration_status()
    assert result["gpu_available"] is False
    assert result["current_encoder"] == "libx264"
    assert result["detection_pending"] is False
