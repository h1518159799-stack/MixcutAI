"""YouTube Data API resumable upload with processing-result verification."""

from __future__ import annotations

import json
import mimetypes
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from db import connect, now
from oauth_token_store import protect, unprotect
from .base import Publisher


API_ROOT = "https://www.googleapis.com/youtube/v3"
UPLOAD_ROOT = "https://www.googleapis.com/upload/youtube/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
RETRIABLE_CODES = {500, 502, 503, 504}


def _json_error(exc: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8", errors="replace"))
        errors = ((payload.get("error") or {}).get("errors") or [])
        return str((errors[0] if errors else {}).get("reason") or (payload.get("error") or {}).get("message") or exc)
    except Exception:
        return str(exc)


def _request_json(url, *, token="", method="GET", body=None, headers=None, timeout=60):
    request_headers = {"Accept": "application/json", **(headers or {})}
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json; charset=UTF-8")
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return (json.loads(raw.decode("utf-8")) if raw else {}), dict(response.headers), response.status
    except urllib.error.HTTPError as exc:
        raw = _json_error(exc)
        raise RuntimeError(_friendly_error(raw)) from exc


def _account_config(account):
    try:
        config = json.loads(account.get("api_config_json") or "{}")
    except (TypeError, ValueError):
        config = {}
    config["client_id"] = str(config.get("client_id") or os.getenv("MIXCUT_YOUTUBE_CLIENT_ID", "")).strip()
    encrypted_secret = str(config.get("client_secret_encrypted") or "").strip()
    try:
        stored_secret = unprotect(encrypted_secret) if encrypted_secret else ""
    except Exception:
        stored_secret = ""
    config["client_secret"] = str(stored_secret or os.getenv("MIXCUT_YOUTUBE_CLIENT_SECRET", "")).strip()
    return config


def _friendly_error(raw: str) -> str:
    lowered = str(raw or "").lower()
    mappings = (
        ("invalid_grant", "YouTube Refresh Token 已失效，请重新授权"),
        ("invalid_client", "YouTube OAuth Client ID 或 Client Secret 无效"),
        ("quotaexceeded", "YouTube API 配额已用完，请稍后重试或检查Google Cloud配额"),
        ("dailylimitexceeded", "YouTube API 当日配额已用完"),
        ("insufficientpermissions", "YouTube OAuth权限不足，请重新授权"),
        ("forbidden", "YouTube API拒绝访问，请检查频道权限和应用审核状态"),
        ("uploadlimitexceeded", "YouTube频道已达到上传限制"),
        ("access_denied", "用户取消了YouTube OAuth授权"),
    )
    compact = lowered.replace("_", "").replace(" ", "")
    for marker, message in mappings:
        if marker.replace("_", "") in compact:
            return message
    return str(raw or "YouTube API请求失败")[:500]


def _access_token(account, diagnostics=None, force_refresh=False):
    diagnostics = diagnostics if diagnostics is not None else {}
    with connect() as conn:
        token_row = conn.execute("SELECT * FROM oauth_tokens WHERE account_id=?", (account["id"],)).fetchone()
    if not token_row:
        raise RuntimeError("YouTube 尚未完成 OAuth 授权")
    token_row = dict(token_row)
    access_token = unprotect(token_row.get("access_token_encrypted") or "")
    expires_at = str(token_row.get("expires_at") or "")
    expired = force_refresh or bool(expires_at and expires_at <= (datetime.now() + timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S"))
    if access_token and not expired and token_row.get("status") == "authorized":
        diagnostics.update({"token_refreshed": False, "token_status": "existing_access_token_valid"})
        return access_token
    refresh_token = unprotect(token_row.get("refresh_token_encrypted") or "")
    config = _account_config(account)
    if not refresh_token:
        raise RuntimeError("YouTube OAuth Token 已过期，请重新授权")
    if not config["client_id"]:
        raise RuntimeError("YouTube OAuth Client ID 未配置")
    payload = {
        "client_id": config["client_id"], "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    if config["client_secret"]:
        payload["client_secret"] = config["client_secret"]
    request = urllib.request.Request(
        TOKEN_URL, data=urllib.parse.urlencode(payload).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            refreshed = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = _json_error(exc)
        diagnostics.update({"token_refreshed": False, "token_status": "refresh_failed", "token_error": raw})
        raise RuntimeError(_friendly_error(raw)) from exc
    access_token = str(refreshed.get("access_token") or "")
    if not access_token:
        raise RuntimeError("YouTube OAuth 刷新失败，请重新登录授权")
    new_expiry = (datetime.now() + timedelta(seconds=int(refreshed.get("expires_in") or 3600))).strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        conn.execute(
            "UPDATE oauth_tokens SET access_token_encrypted=?,expires_at=?,status='authorized',updated_at=? WHERE account_id=?",
            (protect(access_token), new_expiry, now(), account["id"]),
        )
    diagnostics.update({"token_refreshed": True, "token_status": "refresh_succeeded", "token_expire_at": new_expiry})
    return access_token


def verify_youtube_account(account: dict, *, force_refresh=False) -> dict:
    diagnostics = {}
    config = _account_config(account)
    if not config.get("client_id"):
        raise RuntimeError("未配置 YouTube OAuth Client ID")
    if not config.get("client_secret"):
        raise RuntimeError("未配置 YouTube OAuth Client Secret")
    with connect() as conn:
        token = conn.execute("SELECT * FROM oauth_tokens WHERE account_id=? AND provider='youtube'", (account["id"],)).fetchone()
    if not token:
        raise RuntimeError("YouTube账号尚未完成OAuth授权")
    token = dict(token)
    if not token.get("refresh_token_encrypted"):
        raise RuntimeError("YouTube授权没有Refresh Token，请重新授权并允许离线访问")
    access_token = _access_token(account, diagnostics, force_refresh=force_refresh)
    url = API_ROOT + "/channels?" + urllib.parse.urlencode({"part": "id,snippet", "mine": "true"})
    try:
        payload, _, _ = _request_json(url, token=access_token)
    except Exception as exc:
        raise RuntimeError(_friendly_error(str(exc))) from exc
    items = payload.get("items") or []
    if not items:
        raise RuntimeError("授权账号下没有可用的YouTube频道")
    channel = items[0]
    channel_id = str(channel.get("id") or "")
    channel_title = str((channel.get("snippet") or {}).get("title") or "")
    expected = str(account.get("channel_id") or "")
    if expected and expected != channel_id:
        raise RuntimeError("OAuth当前频道与已保存频道不一致，请重新授权")
    diagnostics.update({"channel_id": channel_id, "channel_title": channel_title, "network_check": "passed"})
    return diagnostics


class YouTubePublisher(Publisher):
    platform_code = "youtube"
    integration_type = "api"

    def _start_session(self, video_path, token, body):
        url = UPLOAD_ROOT + "/videos?" + urllib.parse.urlencode({"uploadType": "resumable", "part": "snippet,status"})
        _, headers, _ = _request_json(
            url, token=token, method="POST", body=body,
            headers={
                "X-Upload-Content-Length": str(video_path.stat().st_size),
                "X-Upload-Content-Type": mimetypes.guess_type(video_path.name)[0] or "video/mp4",
            },
        )
        location = headers.get("Location") or headers.get("location")
        if not location:
            raise RuntimeError("YouTube 未返回可恢复上传地址")
        return location

    def _upload_chunks(self, session_url, video_path, token, progress=None):
        total = video_path.stat().st_size
        offset = 0
        chunk_size = 8 * 1024 * 1024
        attempts = 0
        self._upload_retry_count = 0
        with video_path.open("rb") as stream:
            while offset < total:
                stream.seek(offset)
                chunk = stream.read(min(chunk_size, total - offset))
                end = offset + len(chunk) - 1
                request = urllib.request.Request(
                    session_url, data=chunk, method="PUT",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": mimetypes.guess_type(video_path.name)[0] or "video/mp4",
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {offset}-{end}/{total}",
                    },
                )
                try:
                    with urllib.request.urlopen(request, timeout=180) as response:
                        raw = response.read()
                        if response.status in (200, 201):
                            result = json.loads(raw.decode("utf-8")) if raw else {}
                            if progress:
                                progress(100)
                            return result
                        offset = end + 1
                except urllib.error.HTTPError as exc:
                    if exc.code == 308:
                        received = exc.headers.get("Range", "")
                        offset = int(received.rsplit("-", 1)[-1]) + 1 if "-" in received else end + 1
                    elif exc.code in RETRIABLE_CODES and attempts < 5:
                        attempts += 1
                        self._upload_retry_count = attempts
                        time.sleep(random.uniform(0.5, min(8, 2 ** attempts)))
                        continue
                    else:
                        raise RuntimeError("YouTube 视频上传失败：" + _json_error(exc)) from exc
                except (OSError, urllib.error.URLError) as exc:
                    if attempts >= 5:
                        raise RuntimeError(f"YouTube 上传网络中断，重试失败：{exc}") from exc
                    attempts += 1
                    self._upload_retry_count = attempts
                    time.sleep(random.uniform(0.5, min(8, 2 ** attempts)))
                    continue
                if progress:
                    progress(round(offset / total * 100))
        raise RuntimeError("YouTube 上传结束但未返回 video_id")

    def _set_thumbnail(self, video_id, cover_path, token):
        if not cover_path:
            return {"supported": True, "passed": False, "message": "未提供封面"}
        path = Path(cover_path)
        if not path.is_file():
            raise RuntimeError("YouTube 封面文件不存在")
        url = UPLOAD_ROOT + "/thumbnails/set?" + urllib.parse.urlencode({"videoId": video_id, "uploadType": "media"})
        request = urllib.request.Request(
            url, data=path.read_bytes(), method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": mimetypes.guess_type(path.name)[0] or "image/jpeg"},
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError("YouTube 封面上传失败：" + _json_error(exc)) from exc
        return {"supported": True, "passed": bool(payload.get("items")), "message": "封面已上传"}

    def _wait_for_processing(self, video_id, token, timeout=1200):
        deadline = time.time() + timeout
        last = {}
        while time.time() < deadline:
            url = API_ROOT + "/videos?" + urllib.parse.urlencode({
                "part": "snippet,status,processingDetails", "id": video_id,
            })
            payload, _, _ = _request_json(url, token=token)
            items = payload.get("items") or []
            if not items:
                raise RuntimeError("YouTube 后台未找到刚上传的视频")
            last = items[0]
            processing = (last.get("processingDetails") or {}).get("processingStatus")
            upload_status = (last.get("status") or {}).get("uploadStatus")
            if processing == "succeeded" and upload_status not in {"failed", "rejected", "deleted"}:
                return last
            if processing in {"failed", "terminated"} or upload_status in {"failed", "rejected", "deleted"}:
                reason = ((last.get("processingDetails") or {}).get("processingFailureReason")
                          or (last.get("status") or {}).get("failureReason")
                          or (last.get("status") or {}).get("rejectionReason") or "未知原因")
                raise RuntimeError(f"YouTube 平台处理失败：{reason}")
            time.sleep(5)
        raise RuntimeError("YouTube 平台处理超时，未取得最终发布状态")

    def publish(self, video: str, account: dict, metadata: dict):
        started = time.monotonic()
        timings = {}
        token_diagnostics = {}
        video_path = Path(video)
        if not video_path.is_file():
            raise ValueError("待发布视频文件不存在")
        token = _access_token(account, token_diagnostics)
        timings["oauth_seconds"] = round(time.monotonic() - started, 3)
        title = str(metadata.get("title") or "").strip()
        description = str(metadata.get("description") or "").strip()
        tags = [str(item).lstrip("#") for item in metadata.get("tags") or [] if str(item).strip()]
        visibility = str(metadata.get("visibility") or "private")
        if visibility not in {"private", "unlisted", "public"}:
            visibility = "private"
        scheduled_at = str(metadata.get("schedule") or metadata.get("scheduled_at") or "").strip()
        status = {"privacyStatus": "private" if scheduled_at else visibility, "selfDeclaredMadeForKids": False}
        if scheduled_at:
            status["publishAt"] = scheduled_at.replace(" ", "T") + ("Z" if "+" not in scheduled_at and not scheduled_at.endswith("Z") else "")
        body = {"snippet": {"title": title, "description": description, "tags": tags, "categoryId": "22"}, "status": status}
        progress = metadata.get("progress_callback")
        phase = time.monotonic()
        session = self._start_session(video_path, token, body)
        timings["create_resumable_session_seconds"] = round(time.monotonic() - phase, 3)
        phase = time.monotonic()
        uploaded = self._upload_chunks(session, video_path, token, progress)
        timings["upload_seconds"] = round(time.monotonic() - phase, 3)
        video_id = str(uploaded.get("id") or "")
        if not video_id:
            raise RuntimeError("YouTube 上传接口未返回 video_id")
        phase = time.monotonic()
        final = self._wait_for_processing(video_id, token)
        timings["processing_seconds"] = round(time.monotonic() - phase, 3)
        snippet, final_status = final.get("snippet") or {}, final.get("status") or {}
        expected_channel = str(account.get("channel_id") or "")
        checks = {
            "title": snippet.get("title") == title,
            "description": snippet.get("description") == description,
            "tags": set(snippet.get("tags") or []) == set(tags),
            "visibility": final_status.get("privacyStatus") == status["privacyStatus"],
            "channel_id": bool(snippet.get("channelId")) and (not expected_channel or snippet.get("channelId") == expected_channel),
            "processing": (final.get("processingDetails") or {}).get("processingStatus") == "succeeded",
            "scheduled": {"supported": True, "passed": (not scheduled_at or bool(final_status.get("publishAt"))),
                          "message": "已设置" if scheduled_at else "未要求定时发布"},
        }
        mandatory = [checks["title"], checks["description"], checks["tags"], checks["visibility"], checks["channel_id"], checks["processing"]]
        if not all(mandatory):
            raise RuntimeError("YouTube 上传后字段回读校验失败")
        phase = time.monotonic()
        thumbnail_result = self._set_thumbnail(video_id, str(metadata.get("cover_path") or ""), token)
        timings["thumbnail_seconds"] = round(time.monotonic() - phase, 3)
        checks["cover"] = thumbnail_result
        if not thumbnail_result.get("passed"):
            raise RuntimeError("YouTube测试封面上传后未确认成功")
        # Read the resource again after thumbnail upload. The upload response
        # plus a second successful resource read is required for acceptance.
        phase = time.monotonic()
        confirmed = self._wait_for_processing(video_id, token, timeout=120)
        timings["final_readback_seconds"] = round(time.monotonic() - phase, 3)
        confirmed_snippet = confirmed.get("snippet") or {}
        if confirmed_snippet.get("title") != title or (confirmed.get("status") or {}).get("privacyStatus") != "private":
            raise RuntimeError("YouTube封面上传后的最终回读校验失败")
        timings["total_seconds"] = round(time.monotonic() - started, 3)
        return {
            "id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}", "verified": True,
            "processing_status": "succeeded", "privacy_status": final_status.get("privacyStatus"),
            "channel_id": snippet.get("channelId"), "field_results": checks,
            "token_refresh": token_diagnostics, "timings": timings,
            "network_retry": {"retry_count": int(getattr(self, "_upload_retry_count", 0)), "status": "passed"},
            "api_audit_notice": "未通过 YouTube API 审核的项目所上传视频会被限制为 private",
        }
