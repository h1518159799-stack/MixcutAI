# MixcutAI 发布前状态

- 冻结时间：2026-07-29
- 软件版本：1.3.6
- Git 分支：`release-candidate-v1.3.6-20260729`
- 基线提交：`4f999e506b1f1a031a60fb6dc58f774a86ba6ef9`
- 数据库 Schema：14
- 源码备份：`D:\MixcutAI_PreRelease_Backup\MixcutAI-source-20260729-0926.zip`
- 授权服务器：`https://47-107-104-106.sslip.io`
- 更新清单：`https://47-107-104-106.sslip.io/updates/update.json`

## 已完成功能

- 本地素材扫描、缩略图和分类
- 用户模板、镜头编辑、AI文案、TTS、字幕、BGM、音效与转场
- Timeline 合成、自然增色、结尾停留、FFmpeg成片
- 串行批量生成和任务恢复
- 卡密激活、Ed25519签名校验、离线宽限
- 在线更新下载、SHA256校验、数据库备份和更新器回滚
- 发布账号管理、发布排期和平台验收门禁

## 平台状态

| 平台 | 状态 |
|---|---|
| 抖音 | `passed` |
| YouTube | `untested` |
| 快手 | `untested` |
| 小红书 | `untested` |
| 视频号 | `untested` |
| B站 | `untested` |
| TikTok / Instagram / Facebook / X | `untested` |

除抖音外，普通用户不得创建正式发布、批量发布或定时发布任务。

## 发布前已知问题与待验收项

- 正式用户包尚需确认内置 FFmpeg/FFprobe，不能要求用户手动安装。
- 尚未完成全新 Windows 用户或独立虚拟机的无 Python/Node 启动验收。
- 正式授权服务器需执行真实月卡、年卡、永久卡及异常卡密验收。
- 自动更新需执行旧版到 1.3.6 的真实端到端更新与失败回滚测试。
- 最终 ZIP 尚未执行独立目录二次解压、激活、生成、更新和重启验收。
- 当前工作区包含尚未提交的功能修改；正式候选提交在全部测试通过后创建。

