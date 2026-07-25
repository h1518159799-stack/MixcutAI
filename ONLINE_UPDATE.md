# Mixcut AI 在线更新使用说明

## 客户端文件

用户版 ZIP 必须包含：

- `MixcutAI.exe`
- `MixcutUpdater.exe`
- `default_config.json`

`MixcutUpdater.exe` 是客户端运行组件，不是管理员工具。卡密生成器、发布脚本和服务器管理文件不会进入用户 ZIP。

## 首次启用

在发布第一版带更新能力的客户端前，修改 `default_config.json`：

```json
{
  "app_version": "1.2.0",
  "auto_check_update": true,
  "update_manifest_url": "https://update.example.com/mixcut/update.json"
}
```

`update_manifest_url` 必须是公网可访问的 HTTPS 地址。修改后重新运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_release.ps1
```

尚未安装 `MixcutUpdater.exe` 的旧客户需要最后接收一次完整 ZIP。此后可以只走在线更新。

## 发布新版本

1. 同步修改 `default_config.json`、`build_release.ps1` 和 `version_info.txt` 中的版本号。
2. 执行 `build_release.ps1`。
3. 构建脚本会生成：
   - `release/MixcutAI.exe`
   - `release/MixcutUpdater.exe`
   - `server_update/update.json`
4. 将新版 `MixcutAI.exe` 上传到 HTTPS 下载地址。
5. 修改 `server_update/update.json` 的 `download_url` 为真实下载地址。
6. 先确认 EXE 可正常下载，最后上传 `update.json`。

建议服务器发布顺序：新版 EXE -> 验证下载与 SHA256 -> `update.json`。不要先发布 JSON。

## update.json

```json
{
  "version": "1.3.0",
  "title": "Mixcut AI v1.3.0",
  "content": "新增功能和稳定性优化",
  "download_url": "https://update.example.com/mixcut/MixcutAI.exe",
  "sha256": "新版MixcutAI.exe的64位小写SHA256",
  "force_update": false
}
```

设置 `force_update=true` 后，客户必须完成更新才能进入软件。

## 客户端更新目录

- 下载缓存：`%LOCALAPPDATA%\MixcutAI\data\updates`
- 数据库备份：`%LOCALAPPDATA%\MixcutAI\data\backups`
- 更新日志：`%LOCALAPPDATA%\MixcutAI\data\update.log`
- 旧 EXE 备份：主程序同目录下的 `MixcutAI.exe.bak`

## 安全规则

- Manifest 和 EXE 下载都只接受 HTTPS。
- HTTPS 重定向到 HTTP 会被拒绝。
- 下载完成后进行 SHA256 校验。
- 更新器替换前再次进行 SHA256 校验。
- 替换前备份 SQLite 数据库和旧 EXE。
- 新版启动后立即退出时，自动恢复旧 EXE 并重新启动。

