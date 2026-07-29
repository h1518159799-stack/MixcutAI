"""Alibaba Cloud Model Studio (Bailian) non-streaming TTS adapter."""

from __future__ import annotations

import json
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from tts_service import audio_duration


DEFAULT_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer"
DEFAULT_MODEL = "qwen-audio-3.0-tts-flash"
BUILTIN_VOICES = (
    {"name": "龙安欢（自然女声）", "voice_code": "longanhuan_v3.6"},
    {"name": "龙杰力豆（童声）", "voice_code": "longjielidou_v3.6"},
    {"name": "Loongeva（英语女声）", "voice_code": "loongeva_v3.6"},
    {"name": "Loongjohn（英语男声）", "voice_code": "loongjohn"},
)


class BailianTtsError(RuntimeError):
    pass


def _friendly_error(exc: Exception, detail: str = "") -> BailianTtsError:
    text = f"{detail} {exc}".strip()
    code = getattr(exc, "code", None)
    if code in (401, 403) or any(word in text.lower() for word in ("invalidapikey", "invalid api key", "unauthorized")):
        return BailianTtsError("API Key无效")
    if code == 429 or any(word in text.lower() for word in ("rate limit", "throttl", "limit exceeded")):
        return BailianTtsError("请求频率受限，请稍后重试")
    if any(word in text.lower() for word in ("model", "model_not_found")) and any(word in text.lower() for word in ("invalid", "not found", "unavailable")):
        return BailianTtsError("模型不可用")
    if any(word in text.lower() for word in ("voice", "speaker")) and any(word in text.lower() for word in ("invalid", "not found", "unsupported")):
        return BailianTtsError("音色不存在或与当前模型不匹配")
    if isinstance(exc, urllib.error.URLError) or any(word in text.lower() for word in ("timed out", "refused", "name resolution")):
        return BailianTtsError("网络连接失败或接口地址无法访问")
    return BailianTtsError("阿里云百炼配音请求失败" + (f"：{detail[:180]}" if detail else ""))


def validate_config(config: dict) -> None:
    if not str(config.get("api_key") or "").strip():
        raise BailianTtsError("请先填写阿里云百炼 API Key")
    endpoint = str(config.get("base_url") or DEFAULT_ENDPOINT).strip()
    if not endpoint.startswith("https://"):
        raise BailianTtsError("接口地址必须使用 HTTPS")
    if not str(config.get("model") or DEFAULT_MODEL).strip():
        raise BailianTtsError("请填写模型")


def _request_json(endpoint: str, api_key: str, payload: dict, timeout: int) -> dict:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + api_key.strip(), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=max(10, int(timeout or 120))) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise _friendly_error(exc, detail) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise _friendly_error(exc) from exc


def list_voices(_config: dict | None = None) -> list[dict]:
    # Bailian does not expose one universal endpoint for every model's system
    # voices.  Keep the officially documented Qwen-Audio presets locally;
    # custom voices can still be maintained in Mixcut's existing voice table.
    return [dict(item) for item in BUILTIN_VOICES]


def synthesize(
    config: dict,
    text: str,
    voice: str,
    output_path: str,
    *,
    speed: float = 1.0,
    volume: float = 1.0,
    sample_rate: int = 24000,
    audio_format: str = "wav",
) -> str:
    validate_config(config)
    if not str(text or "").strip():
        raise BailianTtsError("试听文案不能为空")
    if not str(voice or "").strip():
        raise BailianTtsError("请选择默认音色")
    endpoint = str(config.get("base_url") or DEFAULT_ENDPOINT).strip()
    payload = {
        "model": str(config.get("model") or DEFAULT_MODEL).strip(),
        "input": {
            "text": str(text).strip(),
            "voice": str(voice).strip(),
            "format": str(audio_format or "wav").lower(),
            "sample_rate": int(sample_rate or 24000),
            "rate": max(0.5, min(2.0, float(speed or 1.0))),
            "volume": max(0, min(100, round(float(volume or 1.0) * 50))),
        },
    }
    data = _request_json(endpoint, str(config["api_key"]), payload, int(config.get("timeout_seconds") or 120))
    if data.get("code") or data.get("message") and not data.get("output"):
        raise _friendly_error(RuntimeError(str(data.get("code") or "API error")), str(data.get("message") or ""))
    audio = ((data.get("output") or {}).get("audio") or {})
    url = str(audio.get("url") or "")
    if not url:
        raise BailianTtsError("阿里云百炼未返回有效音频地址")
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=max(10, int(config.get("timeout_seconds") or 120))) as response:
            target.write_bytes(response.read())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        raise _friendly_error(exc) from exc
    if not target.is_file() or target.stat().st_size == 0:
        raise BailianTtsError("阿里云百炼返回的音频文件为空")
    return str(target)


def get_audio_duration(path: str) -> float:
    duration = float(audio_duration(path) or 0)
    if duration <= 0:
        raise BailianTtsError("生成的语音无法检测真实时长")
    return duration


def test_connection(config: dict, voice: str = "longanhuan_v3.6") -> dict:
    suffix = str(config.get("response_format") or "wav").lower()
    with tempfile.TemporaryDirectory(prefix="mixcut_bailian_test_") as folder:
        output = Path(folder) / f"preview.{suffix}"
        synthesize(config, "你好 这是阿里云百炼配音试听", voice, str(output), audio_format=suffix)
        return {"message": "连接成功，可以正常使用阿里云百炼配音。", "duration": get_audio_duration(str(output))}
