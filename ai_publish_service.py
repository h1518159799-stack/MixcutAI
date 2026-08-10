"""Generate editable publishing metadata through the active text-AI service."""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from ai_services import ServiceError, chat_completion, get_active_text_ai_config
from db import connect, now
from runtime_paths import DATA_DIR


PLATFORM_STRATEGIES = {
    "douyin": "简体中文，标题简洁有钩子，话题3到5个，兼顾行业词和常用泛词",
    "xiaohongshu": "简体中文，真实经验分享感，避免夸张承诺，话题包含搜索型长尾词",
    "kuaishou": "简体中文，口语直接，突出真实人物和工厂实力，话题3到5个",
    "wechat_channels": "简体中文，可信、克制，适合微信生态传播，话题2到4个",
    "bilibili": "简体中文，信息清晰，标题可检索，简介说明视频核心内容",
    "tiktok": "English concise hook with 3 to 5 relevant discoverable hashtags",
    "youtube": "English clear searchable title and concise description with 3 to 5 relevant hashtags",
    "instagram": "English visual lifestyle or business tone with 5 to 8 relevant hashtags",
    "facebook": "English informative trustworthy business tone with 3 to 5 hashtags",
    "x": "English concise conversational title with 2 to 4 hashtags",
}
PLATFORM_LABELS = {
    "douyin": "抖音", "xiaohongshu": "小红书", "kuaishou": "快手",
    "wechat_channels": "视频号", "bilibili": "B站", "tiktok": "TikTok",
    "youtube": "YouTube", "instagram": "Instagram", "facebook": "Facebook", "x": "X",
}


class PublishAIError(RuntimeError):
    """Safe error returned by the publishing AI endpoint."""

    def __init__(self, code: str, user_message: str, log_id: str = ""):
        super().__init__(user_message)
        self.code = code
        self.user_message = user_message
        self.log_id = log_id


def _row(conn, sql: str, args=()) -> dict:
    value = conn.execute(sql, args).fetchone()
    return dict(value) if value else {}


def _sanitize_detail(value: object) -> str:
    text = str(value or "")[:1500]
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)(api[_ -]?key|client[_ -]?secret|access[_ -]?token|refresh[_ -]?token)([\s\"':=]+)[^\s,}\]]+",
        r"\1\2[REDACTED]",
        text,
    )
    return text


def _write_error_log(code: str, detail: object) -> str:
    log_id = "AIP-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2).upper()
    log_dir = Path(DATA_DIR) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "ai_publish.log").open("a", encoding="utf-8") as stream:
        stream.write(f"{datetime.now().isoformat(timespec='seconds')} {log_id} {code} {_sanitize_detail(detail)}\n")
    return log_id


def _friendly_service_error(exc: object) -> PublishAIError:
    detail = str(exc or "")
    lowered = detail.lower()
    if "ai_text_service_not_configured" in lowered:
        code, message = "AI_TEXT_SERVICE_NOT_CONFIGURED", "AI服务尚未配置\n请前往“系统设置 → AI配置”配置并启用文本AI服务。"
    elif "401" in lowered or "invalid_api_key" in lowered or "invalid api key" in lowered or "unauthorized" in lowered:
        code, message = "AI_INVALID_KEY", "AI密钥无效\n请前往“系统设置 → AI配置”重新配置当前AI服务。"
    elif "402" in lowered or "insufficient" in lowered or "quota" in lowered or "balance" in lowered or "余额" in detail:
        code, message = "AI_INSUFFICIENT_BALANCE", "AI服务余额不足\n请检查当前服务商账户余额或配额。"
    elif "429" in lowered or "rate limit" in lowered or "too many requests" in lowered:
        code, message = "AI_RATE_LIMITED", "请求频率过高，请稍后再试。"
    elif "timeout" in lowered or "timed out" in lowered or "超时" in detail:
        code, message = "AI_TIMEOUT", "AI请求超时，请稍后重试。"
    elif "404" in lowered or "model" in lowered and any(word in lowered for word in ("invalid", "not found", "unavailable")):
        code, message = "AI_MODEL_UNAVAILABLE", "当前模型不可用\n请检查系统设置中的模型名称。"
    elif any(word in lowered for word in ("urlerror", "connection refused", "name or service", "unreachable", "connection")) or "连接失败" in detail:
        code, message = "AI_ENDPOINT_UNREACHABLE", "AI接口地址无法访问\n请检查接口地址和网络连接。"
    elif isinstance(exc, (ValueError, json.JSONDecodeError)) or "format" in lowered or "json" in lowered or "空内容" in detail:
        code, message = "AI_RESPONSE_INVALID", "AI返回内容格式异常，请重新生成。"
    else:
        code, message = "AI_REQUEST_FAILED", "AI内容生成失败，请稍后重试。"
    return PublishAIError(code, message, _write_error_log(code, detail))


def _extract_json(text: str) -> dict[str, Any]:
    source = str(text or "").strip()
    start, end = source.find("{"), source.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("AI返回内容中没有有效JSON对象")
    try:
        payload = json.loads(source[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError("AI返回内容格式错误") from exc
    if not isinstance(payload, dict):
        raise ValueError("AI返回内容格式错误")
    return payload


def clean_hashtags(value: Any) -> list[str]:
    values = value if isinstance(value, list) else re.split(r"[\s,，]+", str(value or ""))
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        tag = re.sub(r"[^\w\u4e00-\u9fff-]", "", str(item).strip().lstrip("#"), flags=re.UNICODE)[:40]
        key = tag.casefold()
        if tag and key not in seen:
            seen.add(key)
            result.append(tag)
    return result[:10]


def generate_publish_content(
    video_id: int,
    platform: str,
    account_profile: int | dict | None = None,
    current_title: str = "",
    current_tags: str = "",
    current_cover_text: str = "",
) -> dict[str, Any]:
    platform = str(platform or "douyin").strip().lower()
    if platform not in PLATFORM_STRATEGIES:
        raise PublishAIError("PLATFORM_UNSUPPORTED", "当前平台暂不支持AI发布内容生成。")

    with connect() as conn:
        video = _row(conn, "SELECT * FROM generation_tasks WHERE id=?", (int(video_id),))
        if not video:
            raise PublishAIError("VIDEO_NOT_FOUND", "成片任务不存在，请刷新发布中心后重试。")
        shots = [dict(row) for row in conn.execute(
            """SELECT shot_name,original_text,ai_text FROM template_shots
               WHERE template_id=? ORDER BY sort_order,id""",
            (video.get("template_id"),),
        ).fetchall()]
        if isinstance(account_profile, dict):
            profile = account_profile
        elif account_profile:
            profile = _row(conn, "SELECT * FROM account_profiles WHERE id=?", (int(account_profile),))
        else:
            profile = _row(conn, "SELECT * FROM account_profiles ORDER BY updated_at DESC,id DESC LIMIT 1")

    scripts = [
        {"shot": item.get("shot_name", ""), "original": item.get("original_text", ""), "rewritten": item.get("ai_text", "")}
        for item in shots
    ]
    if not any(item["original"] or item["rewritten"] for item in scripts):
        raise PublishAIError("VIDEO_SCRIPT_EMPTY", "当前视频没有可用于生成发布内容的镜头文案。")

    profile_context = {
        "project_name": video.get("task_name", ""),
        "brand_name": profile.get("company_name", ""),
        "industry": profile.get("industry", ""),
        "product": profile.get("product_info", ""),
        "brand": profile.get("brand_intro", ""),
        "persona": profile.get("persona_info", ""),
    }
    system = (
        "你是短视频发布编辑。必须严格依据视频镜头文案与账号档案生成内容，"
        "不得虚构年限金额资质销量客户数量或效果承诺。只返回严格JSON对象，不要Markdown。"
    )
    user = f"""目标平台：{PLATFORM_LABELS[platform]} ({platform})
平台策略：{PLATFORM_STRATEGIES[platform]}
当前发布标题：{current_title}
当前已有话题：{current_tags}
当前封面文字：{current_cover_text}
账号与产品档案：{json.dumps(profile_context, ensure_ascii=False)}
视频镜头文案：{json.dumps(scripts, ensure_ascii=False)}

返回格式：
{{
  "titles":["候选标题1","候选标题2","候选标题3"],
  "description":"一段可直接发布的简短简介",
  "hashtags":["不带井号的话题1","话题2"],
  "cover_text":"3到8个字的封面标题"
}}
标题返回1到3条且角度不同。话题必须相关，不得声称来自实时热榜。"""

    try:
        config = get_active_text_ai_config()
        payload = _extract_json(chat_completion(
            config,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.72,
        ))
        raw_titles = payload.get("titles") or payload.get("title") or []
        if isinstance(raw_titles, str):
            raw_titles = [raw_titles]
        titles: list[str] = []
        for item in raw_titles:
            title = str(item or "").strip().strip('"“”')[:100]
            if title and title not in titles:
                titles.append(title)
        if not titles:
            raise ValueError("AI返回了空标题")
        description = str(payload.get("description") or "").strip()[:1000]
        hashtags = clean_hashtags(payload.get("hashtags"))
        cover_text = re.sub(
            r"[^\w\u4e00-\u9fff\s-]", "", str(payload.get("cover_text") or "").strip(), flags=re.UNICODE,
        )[:20]
    except PublishAIError:
        raise
    except (ServiceError, ValueError, json.JSONDecodeError, OSError) as exc:
        raise _friendly_service_error(exc) from exc
    except Exception as exc:
        raise _friendly_service_error(exc) from exc

    return {
        "title": titles[0],
        "titles": titles[:3],
        "description": description,
        "hashtags": hashtags,
        "cover_text": cover_text,
        "platform": platform,
        "provider": str(config.get("provider") or ""),
        "model": str(config.get("model") or ""),
        "ai_generated_time": now(),
    }
