# MixcutAI 可迁移源码恢复指南

本备份是完整开发源码，不是用户版 EXE。建议在 Windows 10/11、Python 3.12 或 3.13、Node.js LTS 环境恢复。

## 1. 安装基础工具

安装 Python（勾选 Add Python to PATH）、Node.js/npm、Git，以及包含 `ffmpeg.exe`、`ffprobe.exe` 的 FFmpeg。

验证：

```powershell
python --version
pip --version
node --version
npm --version
ffmpeg -version
ffprobe -version
```

## 2. 解压并进入源码目录

假设解压到 `D:\MixcutAI\ai_mixcut_desktop`：

```powershell
cd D:\MixcutAI\ai_mixcut_desktop
```

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

如需恢复脱敏开发样例（模板、镜头结构、分类等），先关闭软件再执行：

```powershell
New-Item -ItemType Directory -Force .\data | Out-Null
Copy-Item .\backup_data_sanitized\mixcut_sanitized.db .\data\mixcut.db -Force
python -c "from db import init_db; init_db()"
```

`db.py` 会执行现有迁移。不要从旧用户包直接覆盖新电脑数据库。

## 6. 配置 FFmpeg

启动软件后进入“系统设置”，选择新电脑上的 `ffmpeg.exe` 和 `ffprobe.exe`。路径因电脑而异，备份不保存旧电脑绝对路径。

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
