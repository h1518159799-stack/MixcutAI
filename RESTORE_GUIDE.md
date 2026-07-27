# MixcutAI 可迁移源码恢复指南

本备份是完整开发源码，不是用户版 EXE。建议在 Windows 10/11、Python 3.12 或 3.13、Node.js LTS 环境恢复。

## 1. 安装 Git 和基础工具

安装 Git、Python（勾选 Add Python to PATH）、Node.js/npm，以及包含 `ffmpeg.exe`、`ffprobe.exe` 的 FFmpeg。

验证：

```powershell
python --version
pip --version
node --version
npm --version
ffmpeg -version
ffprobe -version
```

## 2. 克隆 Gitee 私有仓库并进入源码目录

在 Gitee 登录/凭据已配置的终端执行：

```powershell
cd D:\MixcutAI
git clone https://gitee.com/brother-jie2/mixcut.git ai_mixcut_desktop
cd D:\MixcutAI\ai_mixcut_desktop
```

也可以将夸克网盘源码 ZIP 解压到同一路径。Git 仓库不包含运行数据、Token、素材库、FFmpeg 二进制和 EXE。

## 3. 创建 Python 虚拟环境并安装依赖

```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-build.txt
```

## 4. 安装前端依赖并构建

```powershell
cd D:\MixcutAI\ai_mixcut_desktop\ui-commercial
npm ci
npm run build
cd ..
```

`npm ci` 必须使用包内 `package-lock.json`，不要复制旧电脑的 `node_modules`。

## 5. 初始化数据库

全新数据库：

```powershell
cd D:\MixcutAI\ai_mixcut_desktop
python -c "from db import init_db; init_db()"
```

Gitee 仓库不包含任何数据库。如已从夸克网盘源码备份包取得脱敏开发样例 `backup_data_sanitized\mixcut_sanitized.db`，可先关闭软件再执行：

```powershell
New-Item -ItemType Directory -Force .\data | Out-Null
Copy-Item .\backup_data_sanitized\mixcut_sanitized.db .\data\mixcut.db -Force
python -c "from db import init_db; init_db()"
```

`db.py` 会执行现有迁移。不要从旧用户包直接覆盖新电脑数据库。

## 6. 配置 FFmpeg

启动软件后进入“系统设置”，选择新电脑上的 `ffmpeg.exe` 和 `ffprobe.exe`。路径因电脑而异，备份不保存旧电脑绝对路径。

FFmpeg 可从官方构建来源重新下载，不应提交到源码仓库。

## 7. 启动软件

```powershell
cd D:\MixcutAI\ai_mixcut_desktop
.\.venv\Scripts\Activate.ps1
python commercial_launcher.py
```

前端单独调试：

```powershell
cd D:\MixcutAI\ai_mixcut_desktop\ui-commercial
npm run dev
```

## 8. 执行测试

```powershell
cd D:\MixcutAI\ai_mixcut_desktop
.\.venv\Scripts\Activate.ps1
python -m compileall -q .
python -m unittest discover -s tests -v
```

## 9. 重新填写配置和授权

复制模板并填写新电脑自己的值：

```powershell
Copy-Item .env.example .env
Copy-Item oauth.example.json oauth.local.json
```

必须重新填写 API Key、OAuth Client ID/Secret、服务器地址，并重新授权 YouTube、抖音及其他平台账号。旧电脑的 DPAPI 数据、Cookie、Token 和 Chromium Profile 未备份，也不应跨机器复用。

## 10. Playwright 浏览器（需要浏览器发布时）

```powershell
python -m playwright install chromium
```

平台登录状态需在新电脑重新建立。

## 10.1 需要单独从夸克网盘保留的内容

- 用户自己的大型素材库和历史成片
- 需要留档的用户数据库备份（必须自行加密保管）
- FFmpeg/FFprobe 离线安装包（无网络恢复时）
- Chromium/Playwright 离线浏览器包（无网络恢复时）
- 发布版 EXE/ZIP（仅作为历史发布物，不用于继续开发）

API Key、OAuth Token、Cookie、密码、许可证私钥不要放入普通网盘源码备份。

## 11. 打包 Windows EXE

确认测试和前端构建通过后：

```powershell
cd D:\MixcutAI\ai_mixcut_desktop
powershell -ExecutionPolicy Bypass -File .\build_release.ps1
```

打包前检查 `.env`、OAuth 凭据、Token、Cookie、许可证私钥没有进入发布目录。

## 12. Git 检查

```powershell
git status
git tag --list portable-backup-20260725
git log -1 --oneline
```

正式依赖以 `requirements.txt`、`requirements-build.txt`、`package.json` 和 `package-lock.json` 为准；`pip_freeze_snapshot.txt` 仅用于排障对照。
