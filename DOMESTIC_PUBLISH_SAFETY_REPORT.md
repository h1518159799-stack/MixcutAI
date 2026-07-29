# 国内平台发布适配器安全检查报告

检查日期：2026-07-28  
检查范围：快手、小红书、视频号、B站  
明确排除：抖音适配器、抖音 Profile、抖音登录检测、抖音成功判定及其测试和配置

## 当前状态

| 平台 | 发布方式 | 状态 | 正式发布门禁 | 真实发布结果 |
|---|---|---|---|---|
| 快手 | 独立 Chromium Profile 浏览器发布 | `untested` | 禁止普通用户正式/批量/定时发布 | 未执行，无作品 ID 或链接 |
| 小红书 | 独立 Chromium Profile 浏览器发布 | `untested` | 禁止普通用户正式/批量/定时发布 | 未执行，无作品 ID 或链接 |
| 视频号 | 独立 Chromium Profile 浏览器发布 | `untested` | 禁止普通用户正式/批量/定时发布 | 未执行，无作品 ID 或链接 |
| B站 | 独立 Chromium Profile 浏览器投稿 | `untested` | 禁止普通用户正式/批量/定时发布 | 未执行，无 BV 号或链接 |
| 抖音 | 既有独立浏览器流程 | `passed` | 保持原状态 | 本轮未检查、未修改 |

## 适配器检查结果

- 四个平台已从包含抖音逻辑的通用浏览器发布器中隔离，改由 `GuardedDomesticBrowserPublisher` 承担未验收平台的保护流程。
- 不使用坐标盲点；上传、标题、简介、封面和提交控件均需通过可见 DOM 选择器确认。
- 页面控件缺失立即返回 `PAGE_CHANGED`，不会继续点击。
- 验证码、扫码、手机确认和人工必选项进入 `needs_manual_action`，不会绕过。
- 浏览器关闭或崩溃进入安全失败，不会自动重发。
- 上传 100%、点击发布、页面跳转、固定等待结束均不能单独判定成功。
- 只有同时取得平台成功信号、真实作品 ID/BV 号和作品链接，才允许写入 `succeeded`。
- 管理员真实验收会保存当前 URL、关键截图、HTML 快照和浏览器日志。

## 发布前检查

`preflight_publish_check()` 已检查：平台验收状态、账号、登录状态、独立 Profile、视频存在/可读、FFprobe 解析、封装/编码、时长、尺寸/比例、标题、简介、话题、封面、定时发布时间、网络、重复任务和适配器加载。

任一检查失败即停止，不进入上传阶段。

## 状态机

新保护流程使用：

`draft → ready → prechecking → uploading → submitting → processing → waiting_confirmation → succeeded`

异常状态：`failed`、`needs_login`、`needs_manual_action`、`cancelled`。

软件重启时，处于上传、提交、处理或待确认的任务不会直接重发，而会恢复为 `needs_manual_action`，要求先到平台后台核对。

## 防重复发布

幂等键由以下字段生成：

- 平台
- 账号 ID
- 视频文件哈希
- 标题哈希
- 定时发布时间

桌面进程内创建任务使用互斥锁；已处于活动状态的相同幂等任务返回 `DUPLICATE_TASK`。连续点击不会创建第二条任务，异常退出后也不会盲目重新提交。

## 错误码

`AUTH_EXPIRED`、`ACCOUNT_NOT_FOUND`、`PLATFORM_UNTESTED`、`FILE_NOT_FOUND`、`FILE_UNREADABLE`、`VIDEO_INVALID`、`VIDEO_TOO_LARGE`、`TITLE_TOO_LONG`、`COVER_INVALID`、`NETWORK_TIMEOUT`、`RATE_LIMITED`、`UPLOAD_FAILED`、`SUBMIT_FAILED`、`PROCESSING_FAILED`、`PAGE_CHANGED`、`CAPTCHA_REQUIRED`、`MANUAL_CONFIRM_REQUIRED`、`DUPLICATE_TASK`、`BROWSER_CRASHED`、`UNKNOWN_ERROR`。

每个错误包含中文标题、中文说明、重试/重新登录/人工处理标记和脱敏技术日志编号。UI 不显示 Python 堆栈或 Playwright 原始异常。

## 自动化测试

- 国内发布安全专项：6 个测试方法，120 个子测试通过。
- 四个平台分别覆盖要求的 17 类异常场景。
- 完整后端测试：97 个测试、120 个子测试全部通过。
- 前端生产构建：Vite 构建通过。
- 自动化测试仅证明异常保护和状态机不会崩溃，不代表平台真实发布已通过。

## 真实验收

本轮没有可用的四个平台真实测试账号，因此没有执行外部发布动作，也没有生成作品 ID、BV 号或作品链接。四个平台继续保持 `untested`。真实验收必须按快手、B站、小红书、视频号的顺序逐个平台执行。

## 截图

- `artifacts/domestic_publish_safety/account_platform_status.png`
- `artifacts/domestic_publish_safety/platform_acceptance_gate.png`

