from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
import wave
from array import array
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from db import APP_DIR, VOICE_DIR, connect
from config_secret_store import runtime_service_config
from tts_service import audio_duration
from voice_clone_service import minimax_endpoint
from aliyun_bailian_tts import synthesize as synthesize_bailian
from runtime_paths import MediaRuntimeError, get_ffmpeg_path


class ServiceError(RuntimeError):
    pass


class AIRequestError(ServiceError):
    """Normalized text-AI failure safe for task logs and UI."""

    def __init__(self, code: str, message: str, *, retryable: bool = False, status_code: int = 0):
        super().__init__(message)
        self.code = str(code or "AI_PROVIDER_ERROR")
        self.retryable = bool(retryable)
        self.status_code = int(status_code or 0)


AI_RETRY_DELAYS = (2.0, 5.0)
AIRetryCallback = Callable[[dict], None]


def _emit_ai_retry(callback: AIRetryCallback | None, **event) -> None:
    if callback is not None:
        callback(dict(event))


def get_active_text_ai_config() -> dict:
    """Return the single active text-AI configuration for backend use only.

    The API key is decrypted here and this object must never be serialized to
    the frontend.  Keeping this lookup next to ``chat_completion`` prevents
    feature pages from inventing their own provider/configuration path.
    """
    with connect() as conn:
        row = conn.execute(
            """SELECT * FROM ai_service_configs
               WHERE is_enabled=1
               ORDER BY is_default DESC,id
               LIMIT 1"""
        ).fetchone()
    if not row:
        raise ServiceError("AI_TEXT_SERVICE_NOT_CONFIGURED")
    config = runtime_service_config(dict(row))
    if not str(config.get("api_key") or "").strip():
        raise ServiceError("AI_TEXT_SERVICE_NOT_CONFIGURED")
    return config


def _endpoint(base_url: str, suffix: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ServiceError("服务地址不能为空")
    return base if base.endswith(suffix) else base + suffix


def normalize_ai_base_url(base_url: str) -> str:
    """Normalize an OpenAI-compatible root without inventing path segments."""
    value = str(base_url or "").strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AIRequestError("AI_CONNECTION_FAILED", "AI 接口地址格式不正确。")
    return value


def _test_error(code: str, base_url: str, model: str = "", status_code: int = 0) -> AIRequestError:
    messages = {
        "AI_CONNECTION_FAILED": f"无法连接 AI 服务：{base_url}。请确认接口地址正确，或本地 AI 服务已经启动。",
        "AI_TIMEOUT": "AI 服务响应超时，请检查服务状态或稍后重试。",
        "AI_AUTH_FAILED": "API Key 无效或已失效，请重新配置。",
        "AI_MODEL_NOT_FOUND": f"接口连接成功，但当前模型不可用：{model}。请检查模型名称。",
        "AI_INVALID_RESPONSE": "AI 接口返回内容格式异常。",
        "AI_PROVIDER_ERROR": f"AI 服务返回 HTTP {status_code}。" if status_code else "AI 服务返回异常。",
        "AI_TEST_CANCELLED": "连接测试已取消。",
    }
    return AIRequestError(code, messages.get(code, "AI 服务测试失败。"), status_code=status_code)


def _connection_preflight(base_url: str, timeout: float = 8.0) -> None:
    parsed = urlparse(base_url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((parsed.hostname, port), timeout=max(1.0, min(10.0, float(timeout)))):
            return
    except (OSError, TimeoutError, socket.timeout) as exc:
        raise _test_error("AI_CONNECTION_FAILED", base_url) from exc


def _test_json_request(url: str, api_key: str, timeout: int, *, payload: dict | None = None) -> dict:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key.strip()
    body = None
    method = "GET"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        method = "POST"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=max(5, int(timeout or 60))) as response:
            raw = response.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw or "{}")
            except json.JSONDecodeError as exc:
                raise _test_error("AI_INVALID_RESPONSE", url) from exc
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000].lower()
        exc.close()
        if exc.code in {401, 403} or "invalid_api_key" in detail:
            raise _test_error("AI_AUTH_FAILED", url, status_code=exc.code) from exc
        if payload is not None and exc.code in {400, 404} and any(token in detail for token in ("model", "not found", "does not exist")):
            raise _test_error("AI_MODEL_NOT_FOUND", url, status_code=exc.code) from exc
        raise _test_error("AI_PROVIDER_ERROR", url, status_code=exc.code) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise _test_error("AI_TIMEOUT", url) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)) or "timed out" in str(exc.reason).lower():
            raise _test_error("AI_TIMEOUT", url) from exc
        raise _test_error("AI_CONNECTION_FAILED", url) from exc
    except (ConnectionError, OSError) as exc:
        raise _test_error("AI_CONNECTION_FAILED", url) from exc


def _json_request(url: str, payload: dict, api_key: str, timeout: int) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key.strip()
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=max(5, int(timeout or 60))) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        exc.close()
        if exc.code in {401, 403}:
            raise AIRequestError("AI_AUTH_FAILED", "AI\u914d\u7f6e\u65e0\u6548\uff0c\u8bf7\u68c0\u67e5\u7cfb\u7edf\u8bbe\u7f6e\u4e2d\u7684AI\u670d\u52a1\u914d\u7f6e\u3002", status_code=exc.code) from exc
        if exc.code == 429:
            raise AIRequestError("AI_RATE_LIMITED", "AI\u8bf7\u6c42\u8fc7\u4e8e\u9891\u7e41\uff0c\u5c06\u81ea\u52a8\u91cd\u8bd5\u3002", retryable=True, status_code=exc.code) from exc
        if exc.code in {502, 503, 504}:
            raise AIRequestError("AI_PROVIDER_ERROR", f"AI\u670d\u52a1\u6682\u65f6\u4e0d\u53ef\u7528\uff08HTTP {exc.code}\uff09\u3002", retryable=True, status_code=exc.code) from exc
        raise AIRequestError("AI_PROVIDER_ERROR", f"AI\u670d\u52a1\u8fd4\u56de HTTP {exc.code}\u3002", status_code=exc.code) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise AIRequestError("AI_TIMEOUT", "AI\u6587\u6848\u751f\u6210\u8d85\u65f6\u3002", retryable=True) from exc
    except urllib.error.URLError as exc:
        reason = str(exc.reason or "")
        if isinstance(exc.reason, (TimeoutError, socket.timeout)) or "timed out" in reason.lower():
            raise AIRequestError("AI_TIMEOUT", "AI\u6587\u6848\u751f\u6210\u8d85\u65f6\u3002", retryable=True) from exc
        raise AIRequestError("AI_NETWORK_ERROR", "AI\u670d\u52a1\u7f51\u7edc\u8fde\u63a5\u5931\u8d25\u3002", retryable=True) from exc
    except ConnectionError as exc:
        raise AIRequestError("AI_NETWORK_ERROR", "AI\u670d\u52a1\u7f51\u7edc\u8fde\u63a5\u5931\u8d25\u3002", retryable=True) from exc
    except OSError as exc:
        # ``urllib`` normally wraps socket failures in ``URLError``, but some
        # Windows socket/provider combinations leak a raw ``OSError``.  Keep it
        # inside the same bounded retry path instead of killing the worker.
        raise AIRequestError("AI_NETWORK_ERROR", "AI\u670d\u52a1\u7f51\u7edc\u8fde\u63a5\u5931\u8d25\u3002", retryable=True) from exc
    except json.JSONDecodeError as exc:
        raise AIRequestError("AI_INVALID_RESPONSE", "AI\u8fd4\u56de\u5185\u5bb9\u683c\u5f0f\u5f02\u5e38\u3002") from exc


def chat_completion(config: dict, messages: list[dict], temperature: float = 0.5, *,
                    attempts: int = 1, retry_callback: AIRetryCallback | None = None) -> str:
    if not config.get("is_enabled", 1):
        raise ServiceError("AI 服务已停用")
    provider = config.get("provider") or "openai_compatible"
    if provider != "openai_compatible":
        raise ServiceError(f"暂不支持的 AI 服务类型：{provider}")
    model = str(config.get("model") or "").strip()
    if not model:
        raise ServiceError("AI 模型不能为空")
    total_attempts = max(1, min(3, int(attempts or 1)))
    data = None
    for attempt in range(1, total_attempts + 1):
        _emit_ai_retry(retry_callback, phase="attempt", attempt=attempt, total=total_attempts)
        try:
            data = _json_request(
                _endpoint(config.get("base_url", ""), "/chat/completions"),
                {"model": model, "messages": messages, "temperature": temperature, "stream": False},
                config.get("api_key", ""),
                config.get("timeout_seconds", 60),
            )
            _emit_ai_retry(retry_callback, phase="success", attempt=attempt, total=total_attempts)
            break
        except AIRequestError as exc:
            if not exc.retryable or attempt >= total_attempts:
                _emit_ai_retry(retry_callback, phase="failed", attempt=attempt, total=total_attempts,
                               code=exc.code, message=str(exc), retryable=exc.retryable)
                raise
            delay = AI_RETRY_DELAYS[min(attempt - 1, len(AI_RETRY_DELAYS) - 1)]
            _emit_ai_retry(retry_callback, phase="retry", attempt=attempt, total=total_attempts,
                           code=exc.code, message=str(exc), delay=delay)
            time.sleep(delay)
    assert data is not None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIRequestError("AI_INVALID_RESPONSE", "AI\u8fd4\u56de\u5185\u5bb9\u683c\u5f0f\u5f02\u5e38\u3002") from exc
    content = str(content or "").strip()
    if not content:
        raise AIRequestError("AI_INVALID_RESPONSE", "AI\u8fd4\u56de\u4e86\u7a7a\u6587\u6848\u3002")
    return content


def test_ai_service(config: dict) -> dict:
    """Test current (possibly unsaved) form values with a low-cost request."""
    if not config.get("is_enabled", 1):
        raise AIRequestError("AI_PROVIDER_ERROR", "AI 服务已停用，请先启用此服务。")
    provider = str(config.get("provider") or "openai_compatible")
    if provider != "openai_compatible":
        raise AIRequestError("AI_PROVIDER_ERROR", f"暂不支持的 AI 服务类型：{provider}")
    base_url = normalize_ai_base_url(config.get("base_url", ""))
    api_key = str(config.get("api_key") or "").strip()
    model = str(config.get("model") or "").strip()
    if not api_key:
        raise AIRequestError("AI_AUTH_FAILED", "尚未配置 API Key。")
    if not model:
        raise AIRequestError("AI_MODEL_NOT_FOUND", "AI 模型不能为空。")
    timeout = max(5, int(config.get("timeout_seconds") or 60))
    started = time.perf_counter()
    _connection_preflight(base_url, min(8, timeout))
    model_validation = "通过"
    method = "models+chat"
    try:
        data = _test_json_request(_endpoint(base_url, "/models"), api_key, timeout)
        model_ids = {
            str(item.get("id") or "") for item in (data.get("data") or [])
            if isinstance(item, dict)
        }
        if model_ids and model not in model_ids:
            raise _test_error("AI_MODEL_NOT_FOUND", base_url, model)
        if not isinstance(data, dict):
            raise _test_error("AI_INVALID_RESPONSE", base_url)
    except AIRequestError as exc:
        # Some compatible providers intentionally omit /models. Only endpoint
        # absence falls back; auth, timeout and server errors remain explicit.
        if exc.code != "AI_PROVIDER_ERROR" or exc.status_code not in {404, 405}:
            if exc.code == "AI_MODEL_NOT_FOUND":
                raise _test_error("AI_MODEL_NOT_FOUND", base_url, model, exc.status_code) from exc
            raise
        method = "chat"
        model_validation = "通过（最小请求）"
    # A models response proves authentication and discovery, but not that the
    # endpoint can actually generate text. Always finish with a tiny request so
    # the UI never reports a false success while real rewrites would time out.
    data = _test_json_request(
        _endpoint(base_url, "/chat/completions"), api_key, timeout,
        payload={
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a connection test."},
                {"role": "user", "content": "Reply with OK."},
            ],
            "temperature": 0,
            "max_tokens": 4,
            "stream": False,
        },
    )
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc2:
        raise _test_error("AI_INVALID_RESPONSE", base_url) from exc2
    if not str(content or "").strip():
        raise _test_error("AI_INVALID_RESPONSE", base_url)
    latency_ms = max(1, round((time.perf_counter() - started) * 1000))
    return {
        "message": "连接成功，可以正常使用当前 AI 服务。",
        "service": str(config.get("name") or "AI 服务"),
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "latency_ms": latency_ms,
        "status": "available",
        "model_validation": model_validation,
        "test_method": method,
    }


def _ffmpeg_path(settings: dict) -> str:
    try:
        return get_ffmpeg_path()
    except MediaRuntimeError as exc:
        raise ServiceError(str(exc)) from exc


def _convert_to_wav(source: Path, output: Path, settings: dict):
    command = [_ffmpeg_path(settings), "-y", "-i", str(source), "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(output)]
    completed = subprocess.run(command, capture_output=True, timeout=180,
                               **background_process_kwargs(command, "tts_audio_convert"))
    if completed.returncode != 0 or not output.exists():
        raise ServiceError("FFmpeg 无法转换外部 TTS 音频")


def _synthesize_edge_tts(text: str, voice_code: str, rate: int, output_mp3: Path, timeout: int,
                         attempts: int = 3) -> None:
    """Run bundled edge-tts directly; release builds do not require Python."""
    import edge_tts

    last_error: Exception | None = None
    for attempt in range(1, max(1, int(attempts)) + 1):
        output_mp3.unlink(missing_ok=True)
        try:
            async def generate() -> None:
                communicate = edge_tts.Communicate(text=text, voice=voice_code, rate=f"{rate:+d}%")
                await communicate.save(str(output_mp3))

            asyncio.run(asyncio.wait_for(generate(), timeout=max(30, timeout)))
            if output_mp3.exists() and output_mp3.stat().st_size > 512:
                return
            raise RuntimeError("未生成有效音频文件")
        except asyncio.CancelledError as exc:
            # Python 3.12 treats CancelledError as BaseException.  Converting it
            # here prevents a TTS cancellation from silently killing the whole
            # generation worker without writing a failed status.
            last_error = RuntimeError("Edge TTS 请求被中断或超时")
            output_mp3.unlink(missing_ok=True)
            if attempt < attempts:
                time.sleep(min(2.0, 0.5 * attempt))
        except Exception as exc:
            last_error = exc
            output_mp3.unlink(missing_ok=True)
            if attempt < attempts:
                time.sleep(min(2.0, 0.5 * attempt))
    raise ServiceError(
        f"Edge TTS 连续 {attempts} 次未返回有效音频：{last_error}。"
        "请检查网络后重试，或在系统设置中切换 MiniMax、阿里云百炼等配音服务。"
    ) from last_error


def synthesize_tts(config: dict, voice: dict, text: str, shot_id: int, settings: dict, speed: float = 1.0) -> tuple[str, float]:
    import time as _perf_time
    _real_tts_started = _perf_time.perf_counter()
    try:
        from generation_performance import event
        event("tts_real_start", 0.0, provider=str(config.get("provider") or ""), shot_id=str(shot_id))
    except ImportError:
        event = None
    text = str(text or "").strip()
    if not text:
        raise ServiceError("文案为空，无法生成语音")
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    provider = config.get("provider") or "edge_tts"
    voice_code = str(voice.get("voice_code") or "").strip()
    if not voice_code:
        raise ServiceError("音色代码不能为空")
    output_wav = VOICE_DIR / f"shot_{shot_id}.wav"
    if provider == "edge_tts":
        output_mp3 = VOICE_DIR / f"shot_{shot_id}.mp3"
        rate = round((float(speed or 1) - 1) * 100)
        output_mp3.unlink(missing_ok=True)
        _synthesize_edge_tts(text, voice_code, rate, output_mp3, int(config.get("timeout_seconds") or 120))
        _convert_to_wav(output_mp3, output_wav, settings)
    elif provider == "openai_compatible":
        response_format = str(config.get("response_format") or "mp3").lower()
        raw_path = VOICE_DIR / f"shot_{shot_id}.{response_format}"
        payload = {"model": config.get("model") or "tts-1", "input": text, "voice": voice_code,
                   "response_format": response_format, "speed": max(0.25, min(4.0, float(speed or 1)))}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "audio/*"}
        if config.get("api_key"):
            headers["Authorization"] = "Bearer " + str(config["api_key"]).strip()
        request = urllib.request.Request(_endpoint(config.get("base_url", ""), "/audio/speech"), data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=max(10, int(config.get("timeout_seconds") or 120))) as response:
                raw_path.write_bytes(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise ServiceError(f"TTS HTTP {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise ServiceError(f"TTS 连接失败：{exc.reason}") from exc
        if response_format == "wav":
            output_wav.write_bytes(raw_path.read_bytes())
        else:
            _convert_to_wav(raw_path, output_wav, settings)
    elif provider == "minimax":
        output_mp3 = VOICE_DIR / f"shot_{shot_id}.mp3"
        payload = {
            "model": config.get("model") or "speech-2.8-hd",
            "text": text,
            "stream": False,
            "language_boost": "auto",
            "output_format": "hex",
            "voice_setting": {
                "voice_id": voice_code,
                "speed": max(0.5, min(2.0, float(speed or 1))),
                "vol": 1,
                "pitch": 0,
            },
            "audio_setting": {
                "sample_rate": 32000,
                "bitrate": 128000,
                "format": "mp3",
                "channel": 1,
            },
        }
        data = _json_request(
            minimax_endpoint(config.get("base_url"), "t2a_v2"), payload,
            config.get("api_key", ""), config.get("timeout_seconds", 120),
        )
        base_resp = data.get("base_resp") or {}
        if int(base_resp.get("status_code") or 0) != 0:
            raise ServiceError(f"MiniMax TTS 失败：{base_resp.get('status_msg') or base_resp.get('status_code')}")
        try:
            audio_bytes = bytes.fromhex(data["data"]["audio"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ServiceError("MiniMax TTS 未返回有效音频") from exc
        output_mp3.write_bytes(audio_bytes)
        _convert_to_wav(output_mp3, output_wav, settings)
    elif provider == "aliyun_bailian":
        response_format = str(config.get("response_format") or "wav").lower()
        if response_format not in {"mp3", "wav", "opus"}:
            response_format = "wav"
        raw_path = VOICE_DIR / f"shot_{shot_id}_bailian.{response_format}"
        synthesize_bailian(
            config,
            text,
            voice_code,
            str(raw_path),
            speed=max(0.5, min(2.0, float(speed or 1))),
            volume=float(settings.get("tts_volume") or settings.get("voice_volume") or 1.0),
            sample_rate=int(settings.get("aliyun_bailian_sample_rate") or 24000),
            audio_format=response_format,
        )
        if response_format == "wav":
            output_wav.write_bytes(raw_path.read_bytes())
        else:
            _convert_to_wav(raw_path, output_wav, settings)
    else:
        raise ServiceError(f"暂不支持的 TTS 服务类型：{provider}")
    duration = audio_duration(str(output_wav))
    if duration <= 0:
        raise ServiceError("生成的语音无法检测真实时长")
    if event:
        event("tts_real_end", _perf_time.perf_counter() - _real_tts_started, shot_id=str(shot_id))
    return str(output_wav), duration


def _trim_pcm_silence(frames: bytes, sample_rate: int, threshold_db: float = -46.0, keep_ms: int = 15) -> bytes:
    """Trim only leading/trailing PCM16 silence while preserving natural pauses."""
    if not frames:
        return frames
    samples = array("h")
    samples.frombytes(frames)
    threshold = max(1, round(32767 * (10 ** (threshold_db / 20))))
    first = next((i for i, value in enumerate(samples) if abs(value) >= threshold), 0)
    last = next((i for i in range(len(samples) - 1, -1, -1) if abs(samples[i]) >= threshold), len(samples) - 1)
    keep = max(0, round(sample_rate * keep_ms / 1000))
    first = max(0, first - keep)
    last = min(len(samples) - 1, last + keep)
    return samples[first:last + 1].tobytes()


def concatenate_tts_wavs(paths: list[str], output_path: str, settings: dict, return_timings: bool = False):
    """Trim boundary silence, then join speech parts with one short controlled gap."""
    sources = [Path(path) for path in paths if path and Path(path).is_file()]
    if not sources:
        raise ServiceError("没有可拼接的分段语音")
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    sample_rate = 24000
    chunks = []
    for source in sources:
        with wave.open(str(source), "rb") as stream:
            if stream.getnchannels() != 1 or stream.getsampwidth() != 2 or stream.getframerate() != sample_rate:
                raise ServiceError("TTS 分段音频格式不一致，无法无损拼接")
            chunks.append(_trim_pcm_silence(stream.readframes(stream.getnframes()), sample_rate))
    gap_ms = max(0, min(500, int(float(settings.get("tts_segment_gap_ms") or 60))))
    gap_frames = b"\x00\x00" * round(sample_rate * gap_ms / 1000)
    timings = []
    cursor = 0.0
    with wave.open(str(output), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        for index, chunk in enumerate(chunks):
            speech_duration = len(chunk) / 2 / sample_rate
            timings.append({"start": round(cursor, 3), "end": round(cursor + speech_duration, 3)})
            stream.writeframes(chunk)
            cursor += speech_duration
            if index < len(chunks) - 1 and gap_frames:
                stream.writeframes(gap_frames)
                cursor += gap_ms / 1000
    duration = audio_duration(str(output))
    if duration <= 0:
        raise ServiceError("拼接后的 TTS 语音时长无效")
    if return_timings:
        return str(output), duration, timings
    return str(output), duration


def test_tts_service(config: dict, voice: dict, settings: dict, text: str = "") -> tuple[str, float]:
    return synthesize_tts(config, voice, text or "这是语音服务连接测试。", 0, settings, 1.0)
from subprocess_utils import background_process_kwargs
