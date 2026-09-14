# MixcutAI 项目聊天记录整理

更新时间：2026-09-07

## 一、项目定位

MixcutAI / Mixcut Studio 是桌面端矩阵视频生成与发布工具，包含：

- 素材管理与扫描
- 智能分割视频
- 单条和批量视频生成
- 任务中心与生成队列
- 发布中心与发布排期
- 平台账号、账号分组和矩阵发布
- AI 文案、标题、话题和镜头改写
- 卡密授权与在线更新
- 内置 FFmpeg / FFprobe

## 二、主要功能开发记录

### 1. 账号与发布中心

- 拆分浏览器登录平台和官方 API 平台。
- 普通用户隐藏 Client ID、Client Secret、App Secret、Scope、Redirect URI 和 Token。
- 管理员单独配置平台接入。
- 增加统一平台选择组件和筛选方式。
- 账号列表保留列表展示，增加平台、账号状态、验收状态和搜索筛选。
- 修复发布中心平台选择不同步、平台下拉框遮挡和发布目标选择问题。

### 2. 账号分组 / 矩阵发布组

- 增加账号分组和分组成员关系。
- 一个账号可以加入多个分组，一个分组可以包含多个平台账号。
- 分组发布创建一个批次，并为每个账号创建独立子任务。
- 被门禁拦截的账号写入批次明细，状态为 `skipped` 或 `blocked`，不会伪装成 `failed`。
- 支持只重试失败账号。
- 删除分组不删除账号，删除账号自动清理成员关系。
- 历史批次保留账号和平台快照。

### 3. 任务中心

- 页面分为当前任务、最近完成、历史记录。
- 当前任务优先展示排队、处理中、失败和需要处理的任务。
- 最近完成最多展示 10 个批次。
- 历史记录使用分页和筛选，不一次性加载全部记录。
- 批次默认收起，展开后查看子任务。
- 右侧预览严格绑定当前选中任务，切换任务时清理旧状态。

### 4. AI 功能

- 发布中心 AI 标题、AI 话题和 AI 优化统一复用镜头改写的后端 AI 服务。
- 前端不读取或发送 API Key。
- AI 失败提示转换为中文，技术详情写入脱敏日志。
- AI 优化全部先显示修改前后对比，用户确认后才应用。

### 5. 视频生成与媒体能力

- 保留单条视频“开始合成”入口。
- 批量生成使用全局串行队列和任务恢复机制。
- 增加生成阶段耗时统计和编码器诊断。
- 支持 CPU `libx264`、NVIDIA `h264_nvenc`、Intel `h264_qsv`、AMD `h264_amf` 检测。
- GPU 编码失败自动回退 CPU，不改变原有生成流程。
- 内置 FFmpeg / FFprobe，客户包不依赖系统 PATH。
- 增加素材扫描、智能分割和输出目录打开相关修复。

### 6. TikTok

- TikTok 登录使用系统正式 Chrome，找不到时回退 Edge。
- 每个账号使用独立 Profile：

  `%LOCALAPPDATA%\MixcutAI\browser_profiles\tiktok\<account_id>\`

- 不保存密码、不复制日常 Chrome Cookie、不关闭用户其他 Chrome。
- 登录和发布阶段复用同一个账号 Profile。
- 发布按钮检测支持中文“发布”和英文 `Post` / `Publish` 多级回退定位。

### 7. 发布路由

- 移除默认 `douyin` 回退。
- 发布任务必须携带有效 `platform`。
- 各平台独立路由到对应 Adapter。
- 账号平台不匹配时拒绝任务并提示“当前账号类型与发布平台不匹配”。
- 错误提示根据任务实际平台显示，不再固定显示抖音错误。

## 三、版本与发布包

当前本地版本：

```text
MixcutAI v1.3.16
```

最新完整客户 ZIP：

```text
D:\科技\codex-redteam-mode-main\codex-redteam-mode-main\ai_mixcut_desktop\release\MixcutAI-v1.3.16-win64.zip
```

最近一次构建校验值：

```text
SHA256: 1A6B535F033B51D43474EE6EBEBF4E216080D0BF5267B30D44DF0457BE8F14A7
```

客户包包含：

- `MixcutAI.exe`
- `MixcutUpdater.exe`
- `runtime\ffmpeg\bin\ffmpeg.exe`
- `runtime\ffmpeg\bin\ffprobe.exe`
- 当前前端生产构建
- 内置 Chromium 运行资源

客户需要完整解压 ZIP，不要只替换 `MixcutAI.exe`。

## 四、桌面测试目录

当前桌面测试目录：

```text
E:\桌面\MixcutAI-v1.3.16
```

当前目录已确认存在主程序、更新器、FFmpeg 和 FFprobe。

旧目录备份：

```text
E:\桌面\MixcutAI-v1.3.16-startup-fix-backup-20260825-173354
```

## 五、启动问题及修复

曾出现 Chromium 显示 `127.0.0.1 ERR_CONNECTION_REFUSED`。

已定位并修复的启动链路问题：

- Chromium Profile 被已有进程接管后，启动进程提前退出。
- 启动器误判浏览器退出，并关闭本地后端。
- 启动阶段 GPU、FFmpeg 和更新检测可能增加启动风险。

当前修复：

- 后端先启动并确认端口监听。
- GPU、NVENC、FFmpeg、性能检测延后执行。
- Chromium Profile 按浏览器路径隔离。
- 增加启动诊断日志。

启动日志：

```text
%LOCALAPPDATA%\MixcutAI\data\logs\startup.log
```

日志包含启动时间、程序路径、后端 PID、端口、浏览器命令和异常信息。

## 六、授权后台

正式授权后台首页：

https://license.ghwc.cn/admin

登录页：

https://license.ghwc.cn/admin/login

不要直接打开：

https://license.ghwc.cn/admin/licenses/generate

该地址是后台表单的 `POST` 接口，浏览器直接打开会返回 `405 Method Not Allowed`，不代表服务器故障。

不要使用旧的 IP SSL 地址作为正式后台入口：

https://47-107-104-106.sslip.io/admin/licenses/generate

换电脑后，客户已激活的授权仍保存在云端，不需要重新给客户授权。只有管理后台登录凭据或本地管理资料需要迁移时，才需要处理新电脑配置。

## 七、授权服务故障记录

曾出现授权网址无法访问，真实根因是服务器 Nginx 服务停止，而不是授权后端停止。

当时状态：

- 授权服务监听 `127.0.0.1:8000`
- Nginx 未运行
- 公网 80 / 443 无监听

已恢复并设置 Nginx 开机启动，随后：

- `https://license.ghwc.cn` 返回 `200 OK`
- `/health` 接口正常

## 八、测试记录

最近一次全量 Python 测试：

```text
363 passed, 120 subtests passed
```

定向启动、媒体和相关测试：

```text
33 passed
```

已验证桌面测试版：

- 后端端口正常监听
- `127.0.0.1` HTTP 返回 `200`
- React 根节点和静态资源正常
- FFmpeg / FFprobe 存在并可检测
- 启动阶段检测已延后

## 九、客户发送说明

发送文件：

```text
MixcutAI-v1.3.16-win64.zip
```

客户操作：

1. 完全关闭旧版 MixcutAI。
2. 将 ZIP 完整解压到新目录。
3. 确认 `MixcutAI.exe` 和 `MixcutUpdater.exe` 在同一目录。
4. 确认 `runtime\ffmpeg\bin` 目录完整存在。
5. 启动 `MixcutAI.exe`。

不要删除：

```text
%LOCALAPPDATA%\MixcutAI
```

该目录通常保存用户数据库、授权状态、配置、素材索引和浏览器 Profile。完整替换程序目录不会主动删除这些用户数据。

## 十、尚需注意

- 不要把 `405 Method Not Allowed` 当作授权服务器宕机；先访问 `/admin`。
- 不要把桌面备份目录发给客户。
- 不要只发送或替换主 EXE。
- 不要删除客户的 `%LOCALAPPDATA%\MixcutAI`。
- 修改线上更新文件前必须校验 manifest、EXE 大小和 SHA256 一致。
- 真实客户环境的授权、素材和历史任务保留情况应以现场验证为准，不能仅凭本机测试推断。
