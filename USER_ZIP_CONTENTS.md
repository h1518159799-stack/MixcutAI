# 用户版 ZIP 内容清单

文件：`release/MixcutAI-v1.2.0-win64.zip`

| 文件 | 用途 |
|---|---|
| `MixcutAI.exe` | Windows 桌面主程序，包含 Python runtime、UI、SQLite、签名验证和内置 Edge TTS 依赖。 |
| `MixcutUpdater.exe` | 客户端更新执行器，仅负责校验、备份、替换、回滚和重启，不包含管理员密钥。 |
| `default_config.json` | 首次运行默认配置，不包含机器相关的媒体组件绝对路径。 |
| `runtime/ffmpeg/bin/` | 内置 `ffmpeg.exe` 与 `ffprobe.exe`，启动后自动定位。 |

构建 allowlist 验证：PASS。

不包含源代码、管理员工具、私钥、数据库、测试素材、测试账号、测试报告、日志或 API Key。
