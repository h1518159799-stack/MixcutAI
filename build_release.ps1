param([string]$FFmpegBin = "")

$ErrorActionPreference = "Stop"

$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = [string]$env:MIXCUT_PYTHON
if (-not $Python -or -not (Test-Path $Python)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}
$Version = "1.3.12"
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
    $ExternalChromiumDir = [string]$env:MIXCUT_CHROMIUM_DIR
    if ($ExternalChromiumDir -and (Test-Path (Join-Path $ExternalChromiumDir "chrome.exe"))) {
        Write-Host "Using existing bundled Chromium: $ExternalChromiumDir" -ForegroundColor DarkCyan
    } else {
        $env:PLAYWRIGHT_BROWSERS_PATH = $BrowserCache
        & $Python -m playwright install chromium
        if ($LASTEXITCODE -ne 0) { throw "Chromium download failed" }
    }

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
    if ($ExternalChromiumDir -and (Test-Path (Join-Path $ExternalChromiumDir "chrome.exe"))) {
        $ChromiumExe = Get-Item (Join-Path $ExternalChromiumDir "chrome.exe")
    } else {
        $ChromiumExe = Get-ChildItem -Path $BrowserCache -Recurse -Filter chrome.exe -File |
            Where-Object { $_.FullName -match "chromium-[^\\]+\\chrome-win" } |
            Select-Object -First 1
    }
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
        $PathFFprobe = Get-Command ffprobe.exe -ErrorAction SilentlyContinue
        if ($PathFFmpeg -and $PathFFprobe -and
            (($PathFFmpeg.Source | Split-Path -Parent) -eq ($PathFFprobe.Source | Split-Path -Parent))) {
            $FFmpegCandidates = @($PathFFmpeg.Source | Split-Path -Parent)
        }
    }
    $ResolvedFFmpegBin = $FFmpegCandidates | Select-Object -First 1
    if (-not $ResolvedFFmpegBin) { throw "FFmpeg/FFprobe source not found. Pass -FFmpegBin <folder>." }
    $BundledFFmpegDir = Join-Path $StageDir "runtime\ffmpeg\bin"
    New-Item -ItemType Directory -Path $BundledFFmpegDir -Force | Out-Null
    Copy-Item (Join-Path $ResolvedFFmpegBin "ffmpeg.exe") $BundledFFmpegDir -Force
    Copy-Item (Join-Path $ResolvedFFmpegBin "ffprobe.exe") $BundledFFmpegDir -Force
    foreach ($MediaBinary in @("ffmpeg.exe", "ffprobe.exe")) {
        $MediaPath = Join-Path $BundledFFmpegDir $MediaBinary
        if (-not (Test-Path $MediaPath)) { throw "Bundled media runtime missing after copy: $MediaBinary" }
        & $MediaPath -version 2>$null | Select-Object -First 1 | Write-Host
        if ($LASTEXITCODE -ne 0) { throw "Bundled media runtime is not executable: $MediaBinary" }
    }
    foreach ($Document in @("使用说明.txt", "CHANGELOG.txt", "LICENSE.txt", "THIRD_PARTY_NOTICES.txt", "VERSION.txt")) {
        Copy-Item (Join-Path $AppDir $Document) (Join-Path $StageDir $Document) -Force
    }
    $UserZip = "$ReleaseDir\MixcutAI-v$Version-win64.zip"
    & $Python (Join-Path $AppDir "release_packager.py") create $StageDir $UserZip
    if ($LASTEXITCODE -ne 0) { throw "UTF-8 release ZIP creation failed" }

    Write-Host "[6/8] Cloud licensing enabled; no signing key is packaged locally." -ForegroundColor Cyan

    Write-Host "[7/8] Verifying package allowlist and forbidden files..." -ForegroundColor Cyan
    & $Python (Join-Path $AppDir "release_packager.py") verify $UserZip
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
        "  使用说明.txt / CHANGELOG.txt / LICENSE.txt / THIRD_PARTY_NOTICES.txt / VERSION.txt",
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
        content = "Keep one long-lived serial generation worker, automatically claim the next queued task, and recover stopped workers"
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
