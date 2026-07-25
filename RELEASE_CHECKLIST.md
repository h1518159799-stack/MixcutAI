# Mixcut AI v1.2.0 发布验收报告

验收时间：2026-07-20  
目标系统：Windows 10 x64（10.0.19045）  
用户包：`release/MixcutAI-v1.2.0-win64.zip`

## 结论

发布构建、授权异常、数据迁移、只读目录、中文路径、DPI 和真实成片链路均已通过。最终 EXE 可在没有 Python PATH 的环境完成内置 Edge TTS、ASS 字幕和 FFmpeg 成片。

严格意义上的“全新且从未安装 Python 的独立 VM”未执行：本机 Windows Sandbox 与 Hyper-V 均为 Disabled。已采用清空 PATH 的依赖隔离方式运行最终 EXE，并完成真实成片，确认运行过程不调用 `python.exe`、`py.exe` 或外部 Edge TTS CLI。

## 验收清单

| # | 验收项 | 结果 | 执行证据 |
|---|---|---|---|
| 1 | 未安装 Python 的 Windows 环境 | 通过（依赖隔离） | PATH 仅保留 Windows/System32，`python_on_path=false`；最终 EXE API 200；8 镜头 TTS/字幕/成片完成。独立 VM 受 Sandbox/Hyper-V Disabled 限制。 |
| 2 | 无旧数据库首次启动 | 通过 | 隔离 `clean-home` 启动后自动创建 `data/mixcut.db`，只显示“产品激活”窗口，不启动业务 UI。 |
| 3 | 无 FFmpeg 配置提示 | 通过 | `preflight.json` 记录 `ffmpeg_available=false`，业务 UI 在确认原生 FFmpeg 提示前不启动。 |
| 4 | 错误卡密 | 通过 | 非 `MXLIC1` 短卡密在无服务器时返回“短卡密需要连接授权服务器”；格式错误被拒绝。 |
| 5 | 过期卡密 | 通过 | Ed25519 签名月卡、过期日 2020-01-01，被判定 `expired`。 |
| 6 | 已绑定其他机器 | 通过 | 签名载荷机器码为 MACHINE_B、当前为 MACHINE_A，返回 `machine_mismatch`。 |
| 7 | 激活后重启 | 通过 | 同一 `license.dat` 重启最终 EXE，业务 UI 正常启动。 |
| 8 | 断网启动 | 通过 | 授权服务器指向不可达 `127.0.0.1:9`，永久签名授权离线启动，API HTTP 200。 |
| 9 | 系统时间向前/向后修改 | 通过 | 向前越过签名有效期判定过期；向后低于 `last_seen_utc` 5 分钟以上判定 `clock_rollback`。 |
| 10 | 中文、空格路径 | 通过 | 从 `中文 空格 只读运行目录/MixcutAI.exe` 启动，API HTTP 200。 |
| 11 | 软件目录只读 | 通过 | ACL 拒绝当前用户写入，写入探针失败，EXE 仍从 `%LOCALAPPDATA%` 读写并正常启动。 |
| 12 | Windows 125%/150%/200% 缩放 | 通过 | Edge scale factor 1.25/1.5/2.0 三档截图生成并人工检查，无导航、顶部栏或核心卡片裁切。 |
| 13 | 素材扫描→配音→字幕→混剪→成片 | 通过 | 1 条素材扫描 2.30 秒；8 镜头生成 8 个语音、8 个 ASS、8 个素材关系；任务 56 秒完成。 |

## 全流程成片验证

- 测试任务：`Release Acceptance Full Render`，任务 ID 26
- 镜头数：8
- 语音文件：8/8
- ASS 字幕：8/8
- 素材匹配：8/8
- 输出：`C:/Users/Administrator/AppData/Local/Temp/MixcutAI-FullFlowRender-20260720/outputs/task_26_final.mp4`
- 视频：H264，1080×1920，27.595638 秒
- 音频：AAC，27.633265 秒
- 音视频时差：0.037627 秒
- 文件大小：12,368,816 bytes
- 抽帧检查：标题底色、标题文字、底部字幕均已实际烧录

字幕仍严格遵循产品既定规则：只以用户文案中的空格切段，不自动在无空格的长中文段中插入换行。长段落需要用户在镜头文案中主动添加空格。

## 数据迁移验证

- 当前 schema：`PRAGMA user_version=5`
- 不覆盖：`CREATE TABLE IF NOT EXISTS` + `ensure_column` 幂等迁移
- 升级前：SQLite Online Backup 到 `data/backups/`
- 备份后：`PRAGMA integrity_check=ok`
- 升级后：再次执行 `integrity_check`
- 失败回滚：保留 `.migration_failed`，再原子恢复升级前备份
- 自动化测试：原始值 `original` → 模拟破坏 → 回滚后恢复 `original`

## DPI 截图与机器证据

- `qa/exe-acceptance-results.json`
- `%TEMP%/MixcutAI QA 20260720_125329/dpi-1.25.png`
- `%TEMP%/MixcutAI QA 20260720_125329/dpi-1.5.png`
- `%TEMP%/MixcutAI QA 20260720_125329/dpi-2.png`
- `qa/full-flow-task-result.json`
- `qa/full-flow-validation.json`
- `qa/full-flow-ffprobe.json`
- `qa/full-flow-frame.jpg`

## 自动化测试

`python -m unittest discover -s tests -v`：24/24 通过。

## 工具执行记录

| preferred_tool | selected_tool | capability_match | risk | fallback_reason |
|---|---|---|---|---|
| Current AI Agent | PowerShell + Python unittest | 构建、进程、SQLite、ACL、API、FFmpeg 全覆盖 | 仅操作隔离 QA 目录和本地测试进程 | 本地工程执行最匹配 |
| Browser MCP | Edge headless | DPI 渲染与截图匹配 | 独立临时 Edge profile | 当前没有可调用 Browser MCP，使用系统 Edge 等价执行 |
| WebFetch | 未使用 | 本次不需要外部内容 | 无 | 验收对象完全本地 |

## 发布哈希

- `MixcutAI.exe`: `FF509CCB0A3809ADD07AF577BE79198C52FDF77EB8A1784C9F67C3DB48A505A9`
- `MixcutUpdater.exe`: `AB606A95AD35F437288EF73850EDD541D818C669650E1036816C4F0236512294`
- `MixcutAI-v1.2.0-win64.zip`: `BF4EB4E743AE3502E8D86464E223EBEFEBEAA64C4C515367368DB4DD0E742DCD`

