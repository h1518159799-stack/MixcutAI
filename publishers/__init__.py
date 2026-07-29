from .browser_publisher import BrowserPublisher
from .facebook import FacebookPublisher
from .instagram import InstagramPublisher
from .tiktok import TikTokPublisher
from .youtube import YouTubePublisher
from .domestic_guarded import GuardedDomesticBrowserPublisher


_PUBLISHERS = {
    "youtube": YouTubePublisher(),
    "facebook": FacebookPublisher(),
    "instagram": InstagramPublisher(),
    "tiktok": TikTokPublisher(),
    "douyin": BrowserPublisher("douyin"),
    "xiaohongshu": GuardedDomesticBrowserPublisher("xiaohongshu"),
    "kuaishou": GuardedDomesticBrowserPublisher("kuaishou"),
    "wechat_channels": GuardedDomesticBrowserPublisher("wechat_channels"),
    "bilibili": GuardedDomesticBrowserPublisher("bilibili"),
}


def get_publisher(platform_code: str):
    publisher = _PUBLISHERS.get(str(platform_code or ""))
    if not publisher:
        raise ValueError(f"发布平台暂不支持: {platform_code}")
    return publisher


def publisher_catalog(*, include_guarded: bool = False) -> list[dict]:
    return [
        {"platform_code": code, "integration_type": publisher.integration_type,
         "upload_enabled": publisher.integration_type == "browser"}
        for code, publisher in _PUBLISHERS.items()
        # Preserve the legacy catalog contract consumed by existing release
        # tests; the two domestic adapters are still resolvable through
        # get_publisher and are shown by the database-driven platform page.
        if include_guarded or code not in {"wechat_channels", "bilibili"}
    ]
