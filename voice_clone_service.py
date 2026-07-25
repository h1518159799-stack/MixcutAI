from __future__ import annotations

import json
import mimetypes
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


class VoiceCloneError(RuntimeError):
    pass


def minimax_endpoint(base_url: str, path: str) -> str:
    base = str(base_url or "https://api.minimaxi.com").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base + "/v1/" + path.lstrip("/")


def _read_json(request: urllib.request.Request, timeout: int) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=max(15, int(timeout or 120))) as response:
            return json.loads(response.read().decode("utf-8", errors="replace") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise VoiceCloneError(f"MiniMax HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise VoiceCloneError(f"MiniMax 连接失败：{exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise VoiceCloneError("MiniMax 返回了无效 JSON") from exc


def _check_response(data: dict, action: str) -> dict:
    base_resp = data.get("base_resp") or {}
    if int(base_resp.get("status_code") or 0) != 0:
        raise VoiceCloneError(f"{action}失败：{base_resp.get('status_msg') or base_resp.get('status_code')}")
    return data


def upload_clone_audio(config: dict, audio_path: str) -> int:
    path = Path(audio_path)
    if not path.is_file():
        raise VoiceCloneError("克隆语音样本不存在")
    if path.suffix.lower() not in {".mp3", ".m4a", ".wav"}:
        raise VoiceCloneError("克隆样本只支持 MP3 M4A WAV")
    if path.stat().st_size > 20 * 1024 * 1024:
        raise VoiceCloneError("克隆样本不能超过 20MB")
    api_key = str(config.get("api_key") or "").strip()
    if not api_key:
        raise VoiceCloneError("请先填写 MiniMax API Key")

    boundary = "----Mixcut" + uuid.uuid4().hex
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    body = bytearray()
    body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\nvoice_clone\r\n".encode())
    body.extend(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
        f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
    )
    body.extend(path.read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        minimax_endpoint(config.get("base_url"), "files/upload"),
        data=bytes(body),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
        method="POST",
    )
    data = _check_response(_read_json(request, config.get("timeout_seconds", 120)), "上传样本")
    try:
        return int(data["file"]["file_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise VoiceCloneError("MiniMax 未返回 file_id") from exc


def clone_minimax_voice(config: dict, audio_path: str, voice_id: str = "", preview_text: str = "") -> dict:
    file_id = upload_clone_audio(config, audio_path)
    code = re.sub(r"[^A-Za-z0-9_-]", "", str(voice_id or ""))
    if not code:
        code = f"MixcutClone_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{7,255}", code) or code.endswith(("-", "_")):
        raise VoiceCloneError("音色 ID 需以英文字母开头，长度至少 8 位，只能包含字母数字横线下划线")
    payload = {
        "file_id": file_id,
        "voice_id": code,
        "model": str(config.get("model") or "speech-2.8-hd"),
        "need_noise_reduction": True,
        "need_volume_normalization": True,
    }
    if str(preview_text or "").strip():
        payload["text"] = str(preview_text).strip()[:1000]
    request = urllib.request.Request(
        minimax_endpoint(config.get("base_url"), "voice_clone"),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + str(config.get("api_key") or "").strip(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    data = _check_response(_read_json(request, config.get("timeout_seconds", 120)), "克隆音色")
    return {"voice_code": code, "file_id": file_id, "demo_audio": data.get("demo_audio") or ""}

