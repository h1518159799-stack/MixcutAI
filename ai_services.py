from __future__ import annotations

import asyncio
import json
import subprocess
import urllib.error
import urllib.request
import wave
from array import array
from pathlib import Path

from db import APP_DIR, VOICE_DIR
from tts_service import audio_duration
from voice_clone_service import minimax_endpoint
from aliyun_bailian_tts import synthesize as synthesize_bailian


class ServiceError(RuntimeError):
    pass


def _endpoint(base_url: str, suffix: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ServiceError("服务地址不能为空")
    return base if base.endswith(suffix) else base + suffix


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
        raise ServiceError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise ServiceError(f"连接失败：{exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise ServiceError("服务返回的不是有效 JSON") from exc


def chat_completion(config: dict, messages: list[dict], temperature: float = 0.5) -> str:
    if not config.get("is_enabled", 1):
        raise ServiceError("AI 服务已停用")
    provider = config.get("provider") or "openai_compatible"
    if provider != "openai_compatible":
        raise ServiceError(f"暂不支持的 AI 服务类型：{provider}")
    model = str(config.get("model") or "").strip()
    if not model:
        raise ServiceError("AI 模型不能为空")
    data = _json_request(
        _endpoint(config.get("base_url", ""), "/chat/completions"),
        {"model": model, "messages": messages, "temperature": temperature, "stream": False},
        config.get("api_key", ""),
        config.get("timeout_seconds", 60),
    )
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ServiceError("AI 服务未返回 choices[0].message.content") from exc
    content = str(content or "").strip()
    if not content:
        raise ServiceError("AI 服务返回了空文案")
    return content


def test_ai_service(config: dict) -> str:
    return chat_completion(config, [{"role": "user", "content": "只回复：连接成功"}], temperature=0)


def _ffmpeg_path(settings: dict) -> str:
    configured = str(settings.get("ffmpeg_path") or "")
    return configured if configured and Path(configured).exists() else "ffmpeg"


def _convert_to_wav(source: Path, output: Path, settings: dict):
    command = [_ffmpeg_path(settings), "-y", "-i", str(source), "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(output)]
    completed = subprocess.run(command, capture_output=True, timeout=180, **background_process_kwargs())
    if completed.returncode != 0 or not output.exists():
        raise ServiceError("FFmpeg 无法转换外部 TTS 音频")


def _synthesize_edge_tts(text: str, voice_code: str, rate: int, output_mp3: Path, timeout: int) -> None:
    """Run bundled edge-tts directly; release builds do not require Python."""
    try:
        import edge_tts

        async def generate() -> None:
            communicate = edge_tts.Communicate(text=text, voice=voice_code, rate=f"{rate:+d}%")
            await communicate.save(str(output_mp3))

        asyncio.run(asyncio.wait_for(generate(), timeout=max(30, timeout)))
    except Exception as exc:
        raise ServiceError(f"Edge TTS 生成失败：{exc}") from exc
    if not output_mp3.exists() or output_mp3.stat().st_size == 0:
        raise ServiceError("Edge TTS 未生成有效音频")


def synthesize_tts(config: dict, voice: dict, text: str, shot_id: int, settings: dict, speed: float = 1.0) -> tuple[str, float]:
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
