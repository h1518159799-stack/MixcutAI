# Mixcut 多平台发布适配器

## 当前范围

- API OAuth：YouTube、Facebook Page、Instagram Business、X
- Playwright 浏览器：TikTok、抖音、小红书、快手、视频号
- 本阶段仅包含账号、授权、登录环境和状态检测；上传与发布尚未启用。

## 统一接口

```python
from publish_service import publish

publish(adapter_code, video, title, tags, schedule)
```

各适配器当前会明确返回“发布流程尚未启用”，下一阶段只需在对应模块实现同一签名。

## OAuth 配置

创建 API 账号时填写公开的 `Client ID` 和回调地址。默认回调地址：

```text
http://127.0.0.1:17866/api/oauth/callback
```

如平台要求 `Client Secret`，通过运行环境变量提供，不写入客户端数据库：

```text
MIXCUT_YOUTUBE_CLIENT_SECRET
MIXCUT_FACEBOOK_CLIENT_SECRET
MIXCUT_INSTAGRAM_CLIENT_SECRET
MIXCUT_X_CLIENT_SECRET
```

Access Token 与 Refresh Token 使用当前 Windows 用户的 DPAPI 加密后写入 `oauth_tokens`。

## 浏览器环境

每个浏览器账号使用：

```text
data/browser_profiles/{platform}/{account}
```

Playwright 调用本机 Chrome 或 Edge，不额外下载 Chromium。
