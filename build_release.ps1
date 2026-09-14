param([string]$FFmpegBin = "")

$ErrorActionPreference = "Stop"

$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = [string]$env:MIXCUT_PYTHON
if (-not $Python -or -not (Test-Path $Python)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}
$Version = (Get-Content (Join-Path $AppDir "VERSION.txt") -Raw -Encoding UTF8).Trim()
$RuntimeVersion = "8.1.2"
$BuildStamp = Get-Date -Format "yyyyMMddHHmmss"
$WorkDir = Join-Path $AppDir ("build\pyinstaller_" + $BuildStamp)
$UpdaterWorkDir = Join-Path $AppDir ("build\updater_" + $BuildStamp)
$ReleaseDir = Join-Path $AppDir "release"
$StageDir = "D:\MCBUILD\pkg_clean_final"
$AdminReleaseDir = Join-Path $AppDir "admin_release"
function Get-Sha256([string]$Path) {
    $value = & $Python -c "import hashlib,sys; h=hashlib.sha256(); f=open(sys.argv[1],'rb'); [h.update(x) for x in iter(lambda:f.read(1048576),b'')]; f.close(); print(h.hexdigest().upper())" $Path
    if ($LASTEXITCODE -ne 0 -or $value -notmatch '^[0-9A-F]{64}$') { throw "SHA256_FAILED: $Path" }
    Write-Host 'SHA256_METHOD_USED=python'; return $value
}

Push-Location $AppDir
try {
    # Preflight: release binaries must not be locked by this application's processes.
    foreach ($Target in @((Join-Path $ReleaseDir 'MixcutAI.exe'), (Join-Path $ReleaseDir 'MixcutUpdater.exe'))) {
        $resolvedTarget = [IO.Path]::GetFullPath($Target)
        $owners = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and ([IO.Path]::GetFullPath($_.ExecutablePath) -eq $resolvedTarget) })
        foreach ($owner in $owners) { Stop-Process -Id $owner.ProcessId -Force -ErrorAction Stop }
        for ($i=0; $i -lt 6 -and (Test-Path $Target); $i++) { try { $s=[IO.File]::Open($Target,'Open','ReadWrite','None'); $s.Close(); break } catch { Start-Sleep -Milliseconds (500 * [math]::Pow(2,$i)) } }
    }
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
        --version-file "$AppDir\updater_version_info.txt" `
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
    $PostFlowSource = Join-Path $AppDir "vendor\postflow"
    if (-not (Test-Path (Join-Path $PostFlowSource "uploader\tencent_uploader\main.py"))) { throw "Bundled PostFlow runtime missing" }
    $TencentUploader = Join-Path $PostFlowSource "uploader\tencent_uploader\main.py"
    if ((Get-Content $TencentUploader -Raw) -notmatch "browser_resolver|BROWSER_SOURCE") { throw "BUILD_FAIL: Tencent uploader browser resolver missing" }
    if (-not (Test-Path (Join-Path $PostFlowSource "browser_resolver.py"))) { throw "BUILD_FAIL: browser_resolver.py missing" }
    foreach ($f in @('python\python.exe','python\python314.dll','conf.py')) { if (-not (Test-Path (Join-Path $PostFlowSource $f))) { throw "PORTABLE_PYTHON_INCOMPLETE: $f" } }
    New-Item -ItemType Directory -Force (Join-Path $StageDir "runtime") | Out-Null
    & robocopy $PostFlowSource (Join-Path $StageDir "runtime\postflow") /E /COPY:DAT /DCOPY:DAT /R:2 /W:1 /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "PostFlow runtime copy failed" }
    Copy-Item (Join-Path $AppDir 'publishers/postflow_confirmation.py') (Join-Path $StageDir 'runtime/postflow/postflow_confirmation.py') -Force
    Copy-Item "$AppDir\default_config.json" "$StageDir\default_config.json" -Force
    if ($ExternalChromiumDir -and (Test-Path (Join-Path $ExternalChromiumDir "chrome.exe"))) {
        $ChromiumExe = Get-Item (Join-Path $ExternalChromiumDir "chrome.exe")
    } else {
        $ChromiumExe = Get-ChildItem -Path $BrowserCache -Recurse -Filter chrome.exe -File |
            Where-Object { $_.FullName -match "chromium-[^\\]+\\chrome-win" } |
            Select-Object -First 1
    }
    if (-not $ChromiumExe) { throw "Downloaded Chromium executable not found" }
    $BundledBrowserDir = Join-Path $StageDir "runtime\browser\chromium"
    New-Item -ItemType Directory -Path $BundledBrowserDir -Force | Out-Null
    Copy-Item -Path (Join-Path $ChromiumExe.Directory.FullName "*") -Destination $BundledBrowserDir -Recurse -Force
    if (-not (Test-Path (Join-Path $BundledBrowserDir "chrome.exe"))) { throw "BUILD_FAIL: embedded Chromium missing" }
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
        # Consume the full native output before selecting its first line.
        # Select-Object -First used to close FFmpeg's pipe early and could turn
        # a healthy binary into a false non-zero release-gate failure.
        $MediaVersionOutput = @(& $MediaPath -version 2>&1)
        $MediaExitCode = $LASTEXITCODE
        if ($MediaVersionOutput.Count -gt 0) { Write-Host ([string]$MediaVersionOutput[0]) }
        if ($MediaExitCode -ne 0) { throw "Bundled media runtime is not executable: $MediaBinary" }
    }
    $RuntimeServerDir = Join-Path $AppDir "server_update\runtime"
    & $Python (Join-Path $AppDir "build_runtime_package.py") $BundledFFmpegDir $RuntimeServerDir $RuntimeVersion
    if ($LASTEXITCODE -ne 0) { throw "Runtime repair package build failed" }
    foreach ($Document in @("使用说明.txt", "CHANGELOG.txt", "LICENSE.txt", "THIRD_PARTY_NOTICES.txt", "VERSION.txt")) {
        Copy-Item (Join-Path $AppDir $Document) (Join-Path $StageDir $Document) -Force
    }
    $RequiredReleaseFiles = @(
        (Join-Path $StageDir "MixcutAI.exe"),
        (Join-Path $StageDir "MixcutUpdater.exe"),
        (Join-Path $StageDir "runtime\ffmpeg\bin\ffmpeg.exe"),
        (Join-Path $StageDir "runtime\ffmpeg\bin\ffprobe.exe"),
        (Join-Path $StageDir "runtime\postflow\postflow_confirmation.py"),
        (Join-Path $StageDir "runtime\postflow\uploader\tencent_uploader\main.py")
    )
    foreach ($RequiredFile in $RequiredReleaseFiles) {
        if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
            throw "Release gate failed, required file is missing: $RequiredFile"
        }
    }
    $ExeHash = Get-Sha256 "$ReleaseDir\MixcutAI.exe"
    $UpdaterHash = Get-Sha256 "$ReleaseDir\MixcutUpdater.exe"
    $UpdateServerDir = Join-Path $AppDir "server_update"
    New-Item -ItemType Directory -Path $UpdateServerDir -Force | Out-Null
    $UpdateJsonPath = Join-Path $UpdateServerDir "update.json"
    & $Python (Join-Path $AppDir "build_update_manifest.py") `
        --version $Version `
        --main-url "https://update.ghwc.cn/MixcutAI-v$Version.exe" `
        --main-sha256 $ExeHash `
        --main-size (Get-Item "$ReleaseDir\MixcutAI.exe").Length `
        --updater-url "https://update.ghwc.cn/MixcutUpdater.exe" `
        --updater-sha256 $UpdaterHash `
        --updater-size (Get-Item "$ReleaseDir\MixcutUpdater.exe").Length `
        --runtime-version $RuntimeVersion `
        --runtime-url "https://update.ghwc.cn/runtime/runtime_manifest.json" `
        --output $UpdateJsonPath
    if ($LASTEXITCODE -ne 0) { throw "Shared update manifest generation failed" }
    $MainExeVersion = (Get-Item "$ReleaseDir\MixcutAI.exe").VersionInfo.ProductVersion
    $UpdaterExeVersion = (Get-Item "$ReleaseDir\MixcutUpdater.exe").VersionInfo.ProductVersion
    $UpdaterRuntimeVersion = (& $Python -c "import updater_main; print(updater_main.UPDATER_VERSION)").Trim()
    & $Python (Join-Path $AppDir "verify_update_protocol.py") `
        --main-version $MainExeVersion `
        --updater-version $UpdaterExeVersion `
        --updater-runtime-version $UpdaterRuntimeVersion `
        --manifest $UpdateJsonPath `
        --source-version (Join-Path $AppDir "VERSION.txt")
    if ($LASTEXITCODE -ne 0) { throw "BUILD FAILED: update protocol versions are inconsistent" }
    $UserZip = "$ReleaseDir\MixcutAI-v$Version-win64.zip"
    & $Python (Join-Path $AppDir "release_packager.py") create $StageDir $UserZip
    if ($LASTEXITCODE -ne 0) { throw "UTF-8 release ZIP creation failed" }

    Write-Host "[6/8] Cloud licensing enabled; no signing key is packaged locally." -ForegroundColor Cyan

    Write-Host "[7/8] Verifying package allowlist and forbidden files..." -ForegroundColor Cyan
    & $Python (Join-Path $AppDir "release_packager.py") verify $UserZip
    if ($LASTEXITCODE -ne 0) { throw "User package verification failed" }

    Write-Host "[8/8] Writing hashes and content manifest..." -ForegroundColor Cyan
    $ZipHash = Get-Sha256 $UserZip
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
    Write-Host "Release: $UserZip" -ForegroundColor Green
    Write-Host "SHA256: $ZipHash" -ForegroundColor Green
} finally {
    Pop-Location
}

