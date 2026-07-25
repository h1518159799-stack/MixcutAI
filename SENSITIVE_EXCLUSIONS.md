# 敏感与运行文件排除清单

源码包明确排除：

- `node_modules`、`.venv`、`venv`、`__pycache__`、`.pytest_cache`、Vite/前端缓存
- `dist`、`build`、`release`、`admin_release`、`outputs`、日志和临时文件
- 原始 `data/`、用户数据库、已生成成片、大型素材
- Chromium/Playwright 浏览器 Profile、Cookie、storage state
- Access Token、Refresh Token、OAuth 本地凭据
- 非模板 `.env`、API Key、Client Secret、数据库密码
- 卡密、`license.dat`、许可证私钥、管理员生成密钥
- QA 运行结果、开发日志、截图快照中的运行敏感数据

允许保留：`.env.example`、`config.example.json`、`oauth.example.json`、公钥、脱敏数据库和测试用小素材。

新电脑不得依赖旧 DPAPI 数据，所有平台账号必须重新授权。
