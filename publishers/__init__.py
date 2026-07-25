from .browser_publisher import BrowserPublisher
from .facebook import FacebookPublisher
from .instagram import InstagramPublisher
from .tiktok import TikTokPublisher
from .youtube import YouTubePublisher


_PUBLISHERS = {
    "youtube": YouTubePublisher(),
    "facebook": FacebookPublisher(),
    "instagram": InstagramPublisher(),
    "tiktok": TikTokPublisher(),
    "douyin": BrowserPublisher("douyin"),
    "xiaohongshu": BrowserPublisher("xiaohongshu"),
    "kuaishou": BrowserPublisher("kuaishou"),
}


def get_publisher(platform_code: str):
    publisher = _PUBLISHERS.get(str(platform_code or ""))
    if not publisher:
        raise ValueError(f"发布平台暂不支持: {platform_code}")
    return publisher


def publisher_catalog() -> list[dict]:
    return [
        {"platform_code": code, "integration_type": publisher.integration_type,
         "upload_enabled": publisher.integration_type == "browser"}
        for code, publisher in _PUBLISHERS.items()
    ]
