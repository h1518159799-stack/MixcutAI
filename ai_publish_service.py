"""Generate editable publishing metadata from video scripts and account knowledge."""

from __future__ import annotations

import json
import re
from typing import Any

from ai_services import chat_completion
from db import connect, now


PLATFORM_STRATEGIES = {
    "douyin": "简体中文 强钩子 标题简短 话题3到5个 兼顾行业词和热门泛词",
    "xiaohongshu": "简体中文 真实经验分享感 避免夸张承诺 话题包含搜索型长尾词",
    "kuaishou": "简体中文 口语直接 突出真实人物和工厂实力 话题3到5个",
    "tiktok": "English concise hook 3 to 5 discoverable English hashtags",
    "youtube": "English clear searchable title and concise description 3 to 5 relevant hashtags",
    "instagram": "English visual lifestyle or business tone 5 to 8 relevant hashtags",
    "facebook": "English informative trustworthy business tone 3 to 5 hashtags",
    "x": "English concise conversational post title with 2 to 4 hashtags",
}
PLATFORM_LABELS = {"douyin": "抖音", "xiaohongshu": "小红书", "kuaishou": "快手", "tiktok": "TikTok", "youtube": "YouTube", "instagram": "Instagram", "facebook": "Facebook", "x": "X"}


def _row(conn, sql: str, args=()) -> dict:
    value = conn.execute(sql, args).fetchone()
    return dict(value) if value else {}


def _extract_json(text: str) -> dict[str, Any]:
    source = str(text or "").strip()
    start, end = source.find("{"), source.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("AI 未返回有效发布内容")
    try:
        payload = json.loads(source[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError("AI 发布内容格式错误，请重新生成") from exc
    if not isinstance(payload, dict):
        raise ValueError("AI 发布内容格式错误")
    return payload


def _clean_tags(value: Any) -> list[str]:
    values = value if isinstance(value, list) else re.split(r"[\s,，#]+", str(value or ""))
    result = []
    for item in values:
        tag = re.sub(r"[^\w\u4e00-\u9fff-]", "", str(item).strip().lstrip("#"), flags=re.UNICODE)[:40]
        if tag and tag.lower() not in {value.lower() for value in result}:
            result.append(tag)
    return result[:10]


def generate_publish_content(video_id: int, platform: str, account_profile: int | dict | None = None,
                             current_title: str = "") -> dict[str, Any]:
    platform = str(platform or "douyin").strip().lower()
    if platform not in PLATFORM_STRATEGIES:
        raise ValueError("暂不支持该平台的AI发布策略")
    with connect() as conn:
        video = _row(conn, "SELECT * FROM generation_tasks WHERE id=?", (int(video_id),))
        if not video:
            raise ValueError("成片任务不存在")
        shots = [dict(row) for row in conn.execute(
            "SELECT shot_name,original_text,ai_text FROM template_shots WHERE template_id=? ORDER BY sort_order,id",
            (video.get("template_id"),),
        ).fetchall()]
        if isinstance(account_profile, dict):
            profile = account_profile
        elif account_profile:
            profile = _row(conn, "SELECT * FROM account_profiles WHERE id=?", (int(account_profile),))
        else:
            profile = _row(conn, "SELECT * FROM account_profiles ORDER BY updated_at DESC,id DESC LIMIT 1")
        config = _row(conn, "SELECT * FROM ai_service_configs WHERE is_enabled=1 ORDER BY is_default DESC,id LIMIT 1")
    if not config:
        raise ValueError("请先在系统设置中配置并启用 AI 服务")
    scripts = [
        {"shot": item.get("shot_name", ""), "original": item.get("original_text", ""), "rewritten": item.get("ai_text", "")}
        for item in shots
    ]
    if not any(item["original"] or item["rewritten"] for item in scripts):
        raise ValueError("该视频没有可用于生成发布内容的镜头文案")
    profile_context = {
        "profile_name": profile.get("company_name", ""),
        "industry": profile.get("industry", ""),
        "product_knowledge": profile.get("product_info", ""),
        "brand_knowledge": profile.get("brand_intro", ""),
        "persona_knowledge": profile.get("persona_info", ""),
    }
    system = (
        "你是多平台短视频发布编辑。严格基于用户提供的镜头文案和账号知识库创作，"
        "不得虚构年限 金额 资质 销量 客户数量或效果承诺。只返回严格JSON对象，不要Markdown。"
    )
    user = f"""目标平台：{PLATFORM_LABELS[platform]} ({platform})
平台策略：{PLATFORM_STRATEGIES[platform]}
当前标题：{current_title}
账号知识库：{json.dumps(profile_context, ensure_ascii=False)}
视频镜头文案：{json.dumps(scripts, ensure_ascii=False)}

返回格式：
{{
  "titles":["候选标题1","候选标题2","候选标题3"],
  "description":"一段可直接发布的短视频简介",
  "hashtags":["不带井号的话题1","话题2"],
  "cover_text":"3到5个字的封面大标题"
}}
候选标题返回3到5条，表达角度要不同。封面文字必须短，不要标点。"""
    payload = _extract_json(chat_completion(
        config,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.72,
    ))
    raw_titles = payload.get("titles") or payload.get("title") or []
    if isinstance(raw_titles, str):
        raw_titles = [raw_titles]
    titles = []
    for item in raw_titles:
        title = str(item or "").strip().strip('"“”')[:100]
        if title and title not in titles:
            titles.append(title)
    if not titles:
        raise ValueError("AI 未生成有效标题")
    description = str(payload.get("description") or "").strip()[:1000]
    hashtags = _clean_tags(payload.get("hashtags"))
    cover_text = re.sub(r"[^\w\u4e00-\u9fff\s-]", "", str(payload.get("cover_text") or "").strip(), flags=re.UNICODE)[:20]
    return {
        "title": titles[0],
        "titles": titles[:5],
        "description": description,
        "hashtags": hashtags,
        "cover_text": cover_text,
        "platform": platform,
        "ai_generated_time": now(),
    }
