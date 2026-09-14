import base64
import subprocess
import tempfile
import wave
import threading
import hashlib
from pathlib import Path

from db import VOICE_DIR, connect
from runtime_paths import MediaRuntimeError, get_ffprobe_path
from subprocess_utils import background_process_kwargs
from task_runtime import run_managed_command

_DURATION_CACHE = {}
_DURATION_CACHE_LOCK = threading.Lock()
_DURATION_STATS = {"audio_duration_cache_hit": 0, "audio_duration_cache_miss": 0, "ffprobe_duration_call": 0}


class TtsError(RuntimeError):
    pass


def clean_powershell_message(text: str) -> str:
    text = (text or "").strip()
    if "系统上未安装语音" in text:
        return "Windows TTS 不可用：系统上未安装可用语音，或当前安全设置不允许调用语音。可先用【导入语音文件】导入 WAV。"
    if "Exception calling" in text:
        return "Windows TTS 调用失败。可先用【导入语音文件】导入 WAV。"
    if text.startswith("#< CLIXML"):
        return "Windows TTS 调用失败。可先用【导入语音文件】导入 WAV。"
    return text or "Windows TTS 生成失败"


def find_ffprobe(configured: str = "") -> str:
    try:
        return get_ffprobe_path()
    except MediaRuntimeError:
        return ""


def audio_duration(path: str, ffprobe_path: str = "") -> float:
    """Read the final encoded audio duration with ffprobe; WAV headers are fallback only."""
    p = Path(path)
    if not p.exists():
        return 0.0
    try:
        st = p.stat()
        key = (str(p.resolve()), st.st_mtime_ns, st.st_size)
        with _DURATION_CACHE_LOCK:
            cached = _DURATION_CACHE.get(key)
        if cached is not None:
            _DURATION_STATS["audio_duration_cache_hit"] += 1
            try:
                from generation_performance import event
                event("audio_duration_cache_hit", 0.0, path_hash=hashlib.sha256(str(p).encode()).hexdigest()[:12])
            except ImportError: pass
            return float(cached)
        _DURATION_STATS["audio_duration_cache_miss"] += 1
        try:
            from generation_performance import event
            event("audio_duration_cache_miss", 0.0, path_hash=hashlib.sha256(str(p).encode()).hexdigest()[:12])
        except ImportError: pass
    except OSError:
        key = None
    ffprobe = find_ffprobe(ffprobe_path)
    if ffprobe:
        try:
            _DURATION_STATS["ffprobe_duration_call"] += 1
            try:
                from generation_performance import event
                event("ffprobe_duration_call", 0.0, path_hash=hashlib.sha256(str(p).encode()).hexdigest()[:12])
            except ImportError: pass
            proc = run_managed_command(
                [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=duration",
                 "-of", "default=nw=1:nk=1", str(p)],
                timeout=30, error_type=TtsError, failure_message="音频时长检测失败",
            )
            try:
                duration = float((proc.stdout or "").strip())
                if duration > 0:
                    value = round(duration, 6)
                    if key is not None:
                        with _DURATION_CACHE_LOCK:
                            _DURATION_CACHE[key] = value
                    return value
            except ValueError:
                pass
        except Exception:
            # WAV can still be measured safely from its header below.
            pass
    if p.suffix.lower() == ".wav":
        try:
            with wave.open(str(p), "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                return round(frames / float(rate), 6) if rate else 0.0
        except Exception:
            return 0.0
    return 0.0


def synthesize_windows_tts(text: str, shot_id: int, voice_name: str = "") -> tuple[str, float]:
    """
    TTS 接口预留实现：优先调用 Windows System.Speech 生成真实 WAV。
    这里不按文字估算时长，生成完成后读取 WAV 文件 duration。
    后续可把这个函数替换为 Edge TTS / 云端 TTS / OpenAI TTS。
    """
    text = (text or "").strip()
    if not text:
        raise TtsError("文案为空，无法生成语音")

    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    out = VOICE_DIR / f"shot_{shot_id}.wav"

    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt", encoding="utf-8") as f:
        f.write(text)
        text_file = Path(f.name)

    ps = f"""
Add-Type -AssemblyName System.Speech
$ErrorActionPreference = 'Stop'
$text = Get-Content -LiteralPath '{text_file}' -Raw -Encoding UTF8
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
if ('{voice_name}'.Trim().Length -gt 0) {{ $s.SelectVoice('{voice_name}') }}
$s.Rate = 0
$s.Volume = 100
$s.SetOutputToWaveFile('{out}')
$s.Speak($text)
$s.Dispose()
"""
    encoded = base64.b64encode(ps.encode("utf-16le")).decode("ascii")
    if out.exists():
        out.unlink()
    try:
        import time as _time
        _tts_started = _time.perf_counter()
        try:
            from generation_performance import event
            event("tts_start")
            event("tts_request_start")
        except ImportError: pass
        proc = run_managed_command(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
            timeout=120, error_type=TtsError, failure_message="Windows TTS 生成失败",
        )
        try:
            event("tts_request_end", _time.perf_counter() - _tts_started)
            event("tts_server_wait", _time.perf_counter() - _tts_started)
            event("tts_download_start")
            event("tts_download_end", 0.0)
        except (NameError, UnboundLocalError): pass
    except Exception as exc:
        raise TtsError(clean_powershell_message(str(exc))) from exc
    finally:
        try:
            text_file.unlink(missing_ok=True)
        except Exception:
            pass

    if not out.exists():
        raise TtsError(clean_powershell_message(proc.stderr or proc.stdout or "Windows TTS 生成失败"))

    try:
        event("audio_probe_start")
    except (NameError, UnboundLocalError): pass
    duration = audio_duration(str(out))
    try:
        event("audio_probe_end", _time.perf_counter() - _tts_started)
    except (NameError, UnboundLocalError): pass
    if duration <= 0:
        raise TtsError(clean_powershell_message(proc.stderr or proc.stdout or "语音文件未生成有效内容，无法检测音频时长"))
    return str(out), duration
