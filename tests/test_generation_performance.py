import json

import generation_performance as performance


def test_generation_performance_writes_stage_timings(tmp_path, monkeypatch):
    target = tmp_path / "generation-performance.json"
    monkeypatch.setattr(performance, "PERFORMANCE_PATH", target)
    monkeypatch.setattr(
        performance,
        "diagnose_encoding",
        lambda: {
            "cpu_percent": 12.5,
            "gpu": "nvenc encoder available",
            "encoders": ["h264_nvenc"],
            "encoding": "software x264",
        },
    )

    performance.start("task-1")
    performance.set_hardware(
        gpu_name="NVIDIA Test GPU", gpu_available=False,
        requested_encoder="h264_nvenc", actual_encoder="libx264",
        fallback=True, fallback_reason="driver error",
    )
    with performance.measure("tts"):
        pass
    with performance.measure("ffmpeg_render"):
        pass
    performance.finish()

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["task_id"] == "task-1"
    assert {step["name"] for step in payload["steps"]} == set(performance.STEP_NAMES)
    durations = {step["name"]: step["duration"] for step in payload["steps"]}
    assert durations["tts"] >= 0
    assert durations["ffmpeg_render"] >= 0
    assert payload["total"] >= 0
    assert payload["diagnostics"]["encoders"] == ["h264_nvenc"]
    assert payload["gpu_name"] == "NVIDIA Test GPU"
    assert payload["requested_encoder"] == "h264_nvenc"
    assert payload["actual_encoder"] == "libx264"
    assert payload["fallback"] is True


def test_diagnose_encoding_reports_supported_hardware_encoders(monkeypatch):
    class Completed:
        stdout = " V..... h264_nvenc NVIDIA NVENC\n V..... hevc_nvenc NVIDIA NVENC\n"
        stderr = ""

    monkeypatch.setattr(performance.subprocess, "run", lambda *args, **kwargs: Completed())
    result = performance.diagnose_encoding("ffmpeg.exe")
    assert result["encoders"] == ["h264_nvenc", "hevc_nvenc"]
    assert result["gpu"] == "nvenc encoder available"
