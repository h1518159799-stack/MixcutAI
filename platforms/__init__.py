"""Database-selected platform adapter registry."""

from .domestic_browser import DouyinBrowserAdapter, KuaishouBrowserAdapter, WechatChannelsBrowserAdapter, XiaohongshuBrowserAdapter
from .bilibili_browser import BilibiliBrowserAdapter
from .facebook import FacebookAdapter
from .instagram import InstagramAdapter
from .tiktok_browser import TikTokBrowserAdapter
from .x import XAdapter
from .youtube import YouTubeAdapter


_ADAPTERS = {
    cls.code: cls()
    for cls in (
        YouTubeAdapter, FacebookAdapter, InstagramAdapter, XAdapter,
        TikTokBrowserAdapter, DouyinBrowserAdapter, XiaohongshuBrowserAdapter,
        KuaishouBrowserAdapter, WechatChannelsBrowserAdapter, BilibiliBrowserAdapter,
    )
}


def get_adapter(code: str):
    adapter = _ADAPTERS.get(str(code or ""))
    if not adapter:
        raise ValueError(f"平台适配器未注册: {code}")
    return adapter


def adapter_catalog() -> list[dict]:
    return [{"code": code, "integration_type": adapter.integration_type, "publish_ready": False} for code, adapter in _ADAPTERS.items()]
