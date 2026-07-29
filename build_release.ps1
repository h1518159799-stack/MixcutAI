param([string]$FFmpegBin = "")

$ErrorActionPreference = "Stop"

$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = "C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if (-not (Test-Path $Python)) { $Python = (Get-Command python).Source }
$Version = "1.3.6"
$BuildStamp = Get-Date -Format "yyyyMMddHHmmss"
$WorkDir = Join-Path $AppDir ("build\pyinstaller_" + $BuildStamp)
$UpdaterWorkDir = Join-Path $AppDir ("build\updater_" + $BuildStamp)
$ReleaseDir = Join-Path $AppDir "release"
$StageDir = Join-Path $AppDir ("build\user_release_" + $BuildStamp)
$AdminReleaseDir = Join-Path $AppDir "admin_release"

Push-Location $AppDir
try {
    Write-Host "[1/8] Building React UI..." -ForegroundColor Cyan
    Push-Location (Join-Path $AppDir "ui-commercial")
    try { npm.cmd run build } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { throw "UI build failed" }

    Write-Host "[2/8] Installing locked build dependencies..." -ForegroundColor Cyan
    & $Python -m pip install -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw "Dependency install failed" }
    $BrowserCache = Join-Path $AppDir "build\playwright-browsers"
    $env:PLAYWRIGHT_BROWSERS_PATH = $BrowserCache
    & $Python -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { throw "Chromium download failed" }

    New-Item -ItemType Directory -Path $ReleaseDir -Force | Out-Null
    New-Item -ItemType Directory -Path $StageDir -Force | Out-Null
    New-Item -ItemType Directory -Path $AdminReleaseDir -Force | Out-Null

    Write-Host "[3/8] Packaging standalone updater..." -ForegroundColor Cyan
    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name MixcutUpdater `
        --icon "$AppDir\brand\mixcut-updater.ico" `
        --distpath $ReleaseDir `
        --workpath $UpdaterWorkDir `
        --specpath (Join-Path $AppDir "build") `
        updater_main.py
    if ($LASTEXITCODE -ne 0) { throw "Updater packaging failed with exit code $LASTEXITCODE" }

    Write-Host "[4/8] Packaging windowed one-file EXE..." -ForegroundColor Cyan
    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name MixcutAI `
        --icon "$AppDir\brand\mixcut.ico" `
        --version-file "$AppDir\version_info.txt" `
        --add-data "$AppDir\ui-commercial\dist;ui-commercial\dist" `
        --add-data "$AppDir\brand;brand" `
        --add-data "$AppDir\default_sfx;default_sfx" `
        --add-data "$AppDir\default_config.json;." `
        --hidden-import tkinter `
        --collect-all edge_tts `
        --collect-all playwright `
        --distpath $ReleaseDir `
        --workpath $WorkDir `
        --specpath (Join-Path $AppDir "build") `
        commercial_launcher.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

    Write-Host "[5/8] Creating minimal user package..." -ForegroundColor Cyan
    Copy-Item "$ReleaseDir\MixcutAI.exe" "$StageDir\MixcutAI.exe" -Force
    Copy-Item "$ReleaseDir\MixcutUpdater.exe" "$StageDir\MixcutUpdater.exe" -Force
    Copy-Item "$AppDir\default_config.json" "$StageDir\default_config.json" -Force
    $ChromiumExe = Get-ChildItem -Path $BrowserCache -Recurse -Filter chrome.exe -File |
        Where-Object { $_.FullName -match "chromium-[^\\]+\\chrome-win" } |
        Select-Object -First 1
    if (-not $ChromiumExe) { throw "Downloaded Chromium executable not found" }
    $BundledBrowserDir = Join-Path $StageDir "browser\chromium"
    New-Item -ItemType Directory -Path $BundledBrowserDir -Force | Out-Null
    Copy-Item -Path (Join-Path $ChromiumExe.Directory.FullName "*") -Destination $BundledBrowserDir -Recurse -Force
    $FFmpegCandidates = @(
        $FFmpegBin,
        $env:FFMPEG_BIN,
        (Join-Path $AppDir "vendor\ffmpeg\bin")
    ) | Where-Object { $_ -and (Test-Path (Join-Path $_ "ffmpeg.exe")) -and (Test-Path (Join-Path $_ "ffprobe.exe")) }
    if (-not $FFmpegCandidates) {
        $PathFFmpeg = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
        if ($PathFFmpeg) { $FFmpegCandidates = @($PathFFmpeg.Source | Split-Path -Parent) }
    }
    $ResolvedFFmpegBin = $FFmpegCandidates | Select-Object -First 1
    if (-not $ResolvedFFmpegBin) { throw "FFmpeg/FFprobe source not found. Pass -FFmpegBin <folder>." }
    $BundledFFmpegDir = Join-Path $StageDir "runtime\ffmpeg\bin"
    New-Item -ItemType Directory -Path $BundledFFmpegDir -Force | Out-Null
    Copy-Item (Join-Path $ResolvedFFmpegBin "ffmpeg.exe") $BundledFFmpegDir -Force
    Copy-Item (Join-Path $ResolvedFFmpegBin "ffprobe.exe") $BundledFFmpegDir -Force
    foreach ($Document in @("README_使用说明.txt", "CHANGELOG.txt", "LICENSE.txt", "THIRD_PARTY_NOTICES.txt", "VERSION.txt")) {
        Copy-Item (Join-Path $AppDir $Document) (Join-Path $StageDir $Document) -Force
    }
    $UserZip = "$ReleaseDir\MixcutAI-v$Version-win64.zip"
    @'
import pathlib, sys, zipfile
stage = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
material_root = stage / "\u6df7\u526a\u7d20\u6750"
for category in ("\u64cd\u4f5c\u8fc7\u7a0b", "\u4ea7\u54c1", "\u4ea7\u54c1\u5c55\u793a", "\u5e97\u5185\u73af\u5883", "\u670d\u52a1\u8fc7\u7a0b", "\u7a7a\u955c", "\u95e8\u5934", "\u4eba\u7269"):
    (material_root / category).mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for path in sorted(stage.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(stage).as_posix()
        if path.is_dir():
            archive.writestr(relative.rstrip("/") + "/", b"")
        else:
            archive.write(path, relative)
'@ | & $Python - $StageDir $UserZip
    if ($LASTEXITCODE -ne 0) { throw "UTF-8 release ZIP creation failed" }

    Write-Host "[6/8] Cloud licensing enabled; no signing key is packaged locally." -ForegroundColor Cyan

    Write-Host "[7/8] Verifying package allowlist and forbidden files..." -ForegroundColor Cyan
    @'
import sys, zipfile
path = sys.argv[1]
allowed_root = {"MixcutAI.exe", "MixcutUpdater.exe", "default_config.json", "README_\u4f7f\u7528\u8bf4\u660e.txt", "CHANGELOG.txt", "LICENSE.txt", "THIRD_PARTY_NOTICES.txt", "VERSION.txt"}
forbidden = (".py", ".db", ".log", ".env", ".key", ".pem", "license_admin", "production_test", "test_")
with zipfile.ZipFile(path) as archive:
    names = [item.filename.replace("\\", "/").strip("/") for item in archive.infolist() if not item.is_dir()]
unexpected = [name for name in names if name not in allowed_root and not name.startswith("browser/chromium/") and not name.startswith("runtime/ffmpeg/")]
if unexpected or not allowed_root.issubset(names):
    raise SystemExit(f"user package allowlist mismatch: {unexpected}")
if "browser/chromium/chrome.exe" not in names:
    raise SystemExit("bundled Chromium executable missing")
for binary in ("runtime/ffmpeg/bin/ffmpeg.exe", "runtime/ffmpeg/bin/ffprobe.exe"):
    if binary not in names:
        raise SystemExit(f"bundled media runtime missing: {binary}")
if any(any(mark in name.lower() for mark in forbidden) for name in names):
    raise SystemExit(f"forbidden user package entry: {names}")
print("PACKAGE_ALLOWLIST_OK", len(names), "files; bundled Chromium present")
'@ | & $Python - $UserZip
    if ($LASTEXITCODE -ne 0) { throw "User package verification failed" }

    Write-Host "[8/8] Writing hashes and content manifest..." -ForegroundColor Cyan
    $ExeHash = (Get-FileHash "$ReleaseDir\MixcutAI.exe" -Algorithm SHA256).Hash
    $UpdaterHash = (Get-FileHash "$ReleaseDir\MixcutUpdater.exe" -Algorithm SHA256).Hash
    $ZipHash = (Get-FileHash $UserZip -Algorithm SHA256).Hash
    @(
        "MixcutAI.exe SHA256 $ExeHash",
        "MixcutUpdater.exe SHA256 $UpdaterHash",
        "MixcutAI-v$Version-win64.zip SHA256 $ZipHash"
    ) | Set-Content "$ReleaseDir\SHA256SUMS.txt" -Encoding ASCII
    @(
        "MixcutAI-v$Version-win64.zip",
        "  MixcutAI.exe",
        "  MixcutUpdater.exe",
        "  default_config.json",
        "  browser/chromium/ (bundled Chromium)",
        "  runtime/ffmpeg/bin/ffmpeg.exe",
        "  runtime/ffmpeg/bin/ffprobe.exe",
        "  README_使用说明.txt / CHANGELOG.txt / LICENSE.txt / THIRD_PARTY_NOTICES.txt / VERSION.txt",
        "  混剪素材/",
        "    操作过程 / 产品 / 产品展示 / 店内环境 / 服务过程 / 空镜 / 门头 / 人物",
        "",
        "Forbidden-file allowlist verification: PASS"
    ) | Set-Content "$ReleaseDir\USER_ZIP_CONTENTS.txt" -Encoding UTF8
    $UpdateServerDir = Join-Path $AppDir "server_update"
    New-Item -ItemType Directory -Path $UpdateServerDir -Force | Out-Null
    $UpdateJson = [ordered]@{
        version = $Version
        title = "Mixcut AI v$Version"
        content = "Preserve final TTS tail, add configurable ending hold, and validate final audio-video duration"
        download_url = "https://47-107-104-106.sslip.io/updates/MixcutAI.exe"
        sha256 = $ExeHash.ToLowerInvariant()
        force_update = $false
    } | ConvertTo-Json
    [System.IO.File]::WriteAllText(
        (Join-Path $UpdateServerDir "update.json"),
        $UpdateJson,
        (New-Object System.Text.UTF8Encoding($false))
    )
    Write-Host "Release: $UserZip" -ForegroundColor Green
    Write-Host "SHA256: $ZipHash" -ForegroundColor Green
} finally {
    Pop-Location
}



