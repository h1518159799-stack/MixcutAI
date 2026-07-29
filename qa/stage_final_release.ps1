param(
    [Parameter(Mandatory=$true)][string]$Stage,
    [Parameter(Mandatory=$true)][string]$FFmpegBin
)
$ErrorActionPreference = "Stop"
$Project = Split-Path -Parent $PSScriptRoot
$ResolvedStage = [IO.Path]::GetFullPath($Stage)
if (-not $ResolvedStage.StartsWith("D:\MixcutAI_Final_Release\", [StringComparison]::OrdinalIgnoreCase)) {
    throw "Stage path is outside D:\MixcutAI_Final_Release"
}
if (Test-Path -LiteralPath $ResolvedStage) {
    throw "Stage already exists: $ResolvedStage"
}
New-Item -ItemType Directory -Force -Path $ResolvedStage | Out-Null
foreach ($File in @("MixcutAI.exe", "MixcutUpdater.exe")) {
    Copy-Item (Join-Path $Project "release\$File") (Join-Path $ResolvedStage $File) -Force
}
foreach ($File in @("default_config.json", "README_使用说明.txt", "CHANGELOG.txt", "LICENSE.txt", "THIRD_PARTY_NOTICES.txt", "VERSION.txt")) {
    Copy-Item (Join-Path $Project $File) (Join-Path $ResolvedStage $File) -Force
}
$Chrome = Get-ChildItem (Join-Path $Project "build\playwright-browsers") -Recurse -Filter chrome.exe -File |
    Where-Object { $_.FullName -match "chromium-[^\\]+\\chrome-win" } | Select-Object -First 1
if (-not $Chrome) { throw "Bundled Chromium is missing" }
$BrowserDir = Join-Path $ResolvedStage "browser\chromium"
New-Item -ItemType Directory -Force -Path $BrowserDir | Out-Null
Copy-Item (Join-Path $Chrome.Directory.FullName "*") $BrowserDir -Recurse -Force
$MediaDir = Join-Path $ResolvedStage "runtime\ffmpeg\bin"
New-Item -ItemType Directory -Force -Path $MediaDir | Out-Null
Copy-Item (Join-Path $FFmpegBin "ffmpeg.exe") $MediaDir -Force
Copy-Item (Join-Path $FFmpegBin "ffprobe.exe") $MediaDir -Force
foreach ($Category in @("操作过程", "产品", "产品展示", "店内环境", "服务过程", "空镜", "门头", "人物")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $ResolvedStage "混剪素材\$Category") | Out-Null
}
Get-ChildItem $ResolvedStage -File -Recurse | Measure-Object Length -Sum
