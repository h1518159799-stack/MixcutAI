from .base import BrowserAdapter


class DouyinBrowserAdapter(BrowserAdapter):
    code = "douyin"
    login_url = "https://creator.douyin.com/"


class XiaohongshuBrowserAdapter(BrowserAdapter):
    code = "xiaohongshu"
    login_url = "https://creator.xiaohongshu.com/"


class KuaishouBrowserAdapter(BrowserAdapter):
    code = "kuaishou"
    login_url = "https://cp.kuaishou.com/"


class WechatChannelsBrowserAdapter(BrowserAdapter):
    code = "wechat_channels"
    login_url = "https://channels.weixin.qq.com/"
