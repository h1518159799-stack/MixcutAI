"""Non-invasive generation performance profiler."""
from __future__ import annotations
import ctypes, json, os, subprocess, threading, time
from datetime import datetime, timezone
from pathlib import Path
from runtime_paths import DATA_DIR, get_ffmpeg_path
from subprocess_utils import background_process_kwargs

PERFORMANCE_PATH = DATA_DIR / "logs" / "generation-performance.json"
PERFORMANCE_HISTORY_DIR = DATA_DIR / "logs" / "performance_history"
_LOCAL = threading.local()
STEP_NAMES = (
    "ai_copywriting", "tts", "subtitle", "material_match",
    "ffmpeg_render", "audio_mix",
)

def _cpu_percent():
    try:
        import psutil
        return round(float(psutil.cpu_percent(interval=0.1)), 1)
    except Exception:
        pass
    if os.name != "nt":
        return None
    try:
        class FileTime(ctypes.Structure):
            _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

        def sample():
            idle, kernel, user = FileTime(), FileTime(), FileTime()
            if not ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
            ):
                raise OSError("GetSystemTimes failed")
            value = lambda item: (int(item.high) << 32) | int(item.low)
            return value(idle), value(kernel), value(user)

        first = sample()
        time.sleep(0.1)
        second = sample()
        idle_delta = second[0] - first[0]
        total_delta = (second[1] - first[1]) + (second[2] - first[2])
        return round(max(0.0, min(100.0, (total_delta - idle_delta) * 100.0 / total_delta)), 1)
    except Exception:
        return None

def diagnose_encoding(ffmpeg=None):
    result = {"cpu_percent": _cpu_percent(), "gpu": "unknown", "encoders": [], "encoding": "software x264"}
    try:
        command = [ffmpeg or get_ffmpeg_path(), "-hide_banner", "-encoders"]
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
                                   **background_process_kwargs(command, "generation_performance_encoder_scan"))
        text = (completed.stdout or "") + "\n" + (completed.stderr or "")
        result["encoders"] = [name for name in ("h264_nvenc", "hevc_nvenc", "cuda") if name in text]
        result["gpu"] = "nvenc encoder available" if any(x in result["encoders"] for x in ("h264_nvenc", "hevc_nvenc")) else ("cuda available" if "cuda" in result["encoders"] else "no supported hardware encoder detected")
    except Exception as exc:
        result["diagnostic_error"] = str(exc)
    return result

class GenerationPerformance:
    def __init__(self, task_id):
        self.task_id, self.started = str(task_id), time.perf_counter()
        self.steps = {name: 0.0 for name in STEP_NAMES}
        self.events = []
        self.diagnostics = diagnose_encoding()
    def add(self, name, duration): self.steps[name] = self.steps.get(name, 0.0) + max(0.0, float(duration))
    def event(self, name, duration=0.0, **meta):
        self.events.append({"name": str(name), "duration": round(max(0.0, float(duration)), 3), **meta})
    def measure(self, name):
        tracker = self
        class Measure:
            def __enter__(self): self.started = time.perf_counter(); return self
            def __exit__(self, *_args): tracker.add(name, time.perf_counter() - self.started)
        return Measure()
    def finish(self, error=""):
        total = round(time.perf_counter() - self.started, 3)
        payload = {"task_id": self.task_id, "time": datetime.now(timezone.utc).isoformat(), "steps": [{"name": n, "duration": round(v, 3)} for n, v in self.steps.items()], "events": self.events, "total": total, "diagnostics": self.diagnostics}
        payload.update({
            "gpu_name": self.diagnostics.get("gpu_name", "未检测到"),
            "gpu_memory": self.diagnostics.get("gpu_memory", "未知"),
            "gpu_available": bool(self.diagnostics.get("gpu_available", False)),
            "requested_encoder": self.diagnostics.get("requested_encoder", "libx264"),
            "actual_encoder": self.diagnostics.get("actual_encoder", self.diagnostics.get("encoding", "libx264")),
            "hardware_acceleration": bool(self.diagnostics.get("hardware_acceleration", False)),
            "fallback": bool(self.diagnostics.get("fallback", False)),
            "fallback_reason": self.diagnostics.get("fallback_reason", ""),
        })
        if error: payload["error"] = error
        try:
            PERFORMANCE_PATH.parent.mkdir(parents=True, exist_ok=True)
            PERFORMANCE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            PERFORMANCE_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
            history = {"task_id": self.task_id, "time": payload["time"], "total": total,
                       "steps": payload["steps"], "events": payload["events"]}
            target = PERFORMANCE_HISTORY_DIR / f"{int(time.time()*1000)}-{self.task_id}.json"
            target.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
            files = sorted(PERFORMANCE_HISTORY_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in files[10:]:
                try: old.unlink()
                except OSError: pass
        except OSError: pass

class _NullMeasure:
    def __enter__(self): return self
    def __exit__(self, *_args): return False

def start(task_id):
    _LOCAL.tracker = GenerationPerformance(task_id)

def current(): return getattr(_LOCAL, "tracker", None)
def set_encoding(encoder):
    tracker = current()
    if tracker:
        tracker.diagnostics["encoding"] = str(encoder)
        tracker.diagnostics["actual_encoder"] = str(encoder)
def set_hardware(*, gpu_name="", gpu_memory="", gpu_available=False, requested_encoder="libx264", actual_encoder="libx264", fallback=False, fallback_reason=""):
    tracker = current()
    if not tracker:
        return
    tracker.diagnostics.update({
        "gpu_name": str(gpu_name or "未检测到"),
        "gpu_memory": str(gpu_memory or "未知"),
        "gpu_available": bool(gpu_available),
        "requested_encoder": str(requested_encoder or "libx264"),
        "actual_encoder": str(actual_encoder or "libx264"),
        "hardware_acceleration": str(actual_encoder or "libx264") != "libx264",
        "fallback": bool(fallback),
        "fallback_reason": str(fallback_reason or ""),
    })
def measure(name):
    tracker = current()
    return tracker.measure(name) if tracker else _NullMeasure()

def event(name, duration=0.0, **meta):
    tracker = current()
    if tracker:
        tracker.event(name, duration, **meta)
def finish(error=""):
    tracker = current()
    if tracker:
        tracker.finish(error); _LOCAL.tracker = None
