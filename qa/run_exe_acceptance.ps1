$ErrorActionPreference = "Stop"

$AppDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = "C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$Exe = Join-Path $AppDir "release\MixcutAI.exe"
$Edge = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
$Ffmpeg = "D:\Video\videomix\resources\backend\_internal\static\ffmpeg\bin\ffmpeg.exe"
$Ffprobe = "D:\Video\videomix\resources\backend\_internal\static\ffmpeg\bin\ffprobe.exe"
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$Root = Join-Path $env:TEMP ("MixcutAI QA " + $Stamp)
$ChineseFolder = (-join @([char]0x4E2D,[char]0x6587)) + " " + (-join @([char]0x7A7A,[char]0x683C)) + " " + (-join @([char]0x53EA,[char]0x8BFB,[char]0x8FD0,[char]0x884C,[char]0x76EE,[char]0x5F55))
$RunDir = Join-Path $Root $ChineseFolder
$CleanHome = Join-Path $Root "clean-home"
$NoFfmpegHome = Join-Path $Root "no-ffmpeg-home"
$ActiveHome = Join-Path $Root "active-home"
$ResultsPath = Join-Path $AppDir "qa\exe-acceptance-results.json"
$OriginalPath = $env:PATH
$results = [ordered]@{ timestamp=(Get-Date).ToString("s"); root=$Root }

function Stop-TestApp([string]$profileFragment) {
    $roots = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq "msedge.exe" -and $_.CommandLine -like ("*" + $profileFragment + "*") -and $_.CommandLine -like "*--app=http://127.0.0.1:*"
    }
    foreach ($item in $roots) { Stop-Process -Id $item.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    $apps = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq "MixcutAI.exe" }
    foreach ($item in $apps) { Stop-Process -Id $item.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Find-AppEdge([string]$profileFragment) {
    return Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq "msedge.exe" -and $_.CommandLine -like ("*" + $profileFragment + "*") -and $_.CommandLine -like "*--app=http://127.0.0.1:*"
    } | Select-Object -First 1
}

New-Item -ItemType Directory -Path $RunDir -Force | Out-Null
Copy-Item $Exe (Join-Path $RunDir "MixcutAI.exe") -Force
Copy-Item (Join-Path $AppDir "default_config.json") (Join-Path $RunDir "default_config.json") -Force
$TestExe = Join-Path $RunDir "MixcutAI.exe"

try {
    # Clean first launch with no Python on PATH and no old DB.
    $env:MIXCUT_APP_HOME = $CleanHome
    $env:PATH = "$env:WINDIR\System32;$env:WINDIR"
    $pythonVisible = [bool](Get-Command python -ErrorAction SilentlyContinue)
    $clean = Start-Process -FilePath $TestExe -PassThru
    Start-Sleep -Seconds 8
    $titles = @(Get-Process MixcutAI -ErrorAction SilentlyContinue | ForEach-Object MainWindowTitle | Where-Object { $_ })
    $results.no_python_clean_start = [ordered]@{
        pass=(-not $pythonVisible -and (Test-Path (Join-Path $CleanHome "data\mixcut.db")) -and -not (Find-AppEdge $CleanHome))
        python_on_path=$pythonVisible
        database_created=(Test-Path (Join-Path $CleanHome "data\mixcut.db"))
        window_titles=$titles
    }
    Stop-TestApp $CleanHome

    # Missing FFmpeg must block before business UI and show a native warning.
    $env:PATH = $OriginalPath
    & $Python (Join-Path $AppDir "qa\prepare_qa_home.py") --app-home $NoFfmpegHome
    if ($LASTEXITCODE -ne 0) { throw "No-FFmpeg QA home preparation failed" }
    $env:MIXCUT_APP_HOME = $NoFfmpegHome
    $env:PATH = "$env:WINDIR\System32;$env:WINDIR"
    Start-Process -FilePath $TestExe | Out-Null
    Start-Sleep -Seconds 8
    $ffTitles = @(Get-Process MixcutAI -ErrorAction SilentlyContinue | ForEach-Object MainWindowTitle | Where-Object { $_ })
    $preflightPath = Join-Path $NoFfmpegHome "data\preflight.json"
    $preflight = if(Test-Path $preflightPath){Get-Content $preflightPath -Raw | ConvertFrom-Json}else{$null}
    $results.missing_ffmpeg_prompt = [ordered]@{
        pass=([bool]$preflight -and -not $preflight.ffmpeg_available -and -not (Find-AppEdge $NoFfmpegHome))
        window_titles=$ffTitles
        preflight=$preflight
    }
    Stop-TestApp $NoFfmpegHome

    # Signed activation, offline server, Chinese/space path, read-only install directory.
    $env:PATH = $OriginalPath
    & $Python (Join-Path $AppDir "qa\prepare_qa_home.py") --app-home $ActiveHome --ffmpeg $Ffmpeg --ffprobe $Ffprobe --server-url "http://127.0.0.1:9"
    if ($LASTEXITCODE -ne 0) { throw "Activated QA home preparation failed" }
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls $RunDir /deny "${identity}:(W)" /T /C | Out-Null
    $writeDenied = $false
    try { Set-Content (Join-Path $RunDir "should-not-write.tmp") "x" -ErrorAction Stop } catch { $writeDenied = $true }
    $env:MIXCUT_APP_HOME = $ActiveHome
    $env:PATH = "$env:WINDIR\System32;$env:WINDIR"
    Start-Process -FilePath $TestExe | Out-Null
    Start-Sleep -Seconds 16
    $edgeProcess = Find-AppEdge $ActiveHome
    $url = ""
    $http = 0
    if ($edgeProcess -and $edgeProcess.CommandLine -match "--app=(http://127\.0\.0\.1:\d+/)") {
        $url = $matches[1]
        $http = (Invoke-WebRequest -UseBasicParsing -Uri ($url + "api/health") -TimeoutSec 5).StatusCode
    }
    $results.activated_offline_readonly_chinese_path = [ordered]@{
        pass=([bool]$edgeProcess -and $http -eq 200 -and $writeDenied)
        python_on_path=[bool](Get-Command python -ErrorAction SilentlyContinue)
        read_only_verified=$writeDenied
        api_http=$http
        url=$url
    }

    # Browser rendering at common Windows scale factors.
    $dpi = [ordered]@{}
    foreach ($scale in @(1.25,1.5,2.0)) {
        $shot = Join-Path $Root ("dpi-" + $scale + ".png")
        $profile = Join-Path $Root ("edge-dpi-" + $scale)
        $savedPreference = $ErrorActionPreference
        $ErrorActionPreference = "SilentlyContinue"
        & $Edge --headless=new --disable-gpu --force-device-scale-factor=$scale --window-size=1440,920 --user-data-dir=$profile --screenshot=$shot $url 2>$null | Out-Null
        $ErrorActionPreference = $savedPreference
        $dpi[[string]$scale] = [ordered]@{ pass=(Test-Path $shot); screenshot=$shot; size=if(Test-Path $shot){(Get-Item $shot).Length}else{0} }
    }
    $results.dpi_scaling = $dpi
    Stop-TestApp $ActiveHome

    # Restart with the same signed license.dat while the server remains unreachable.
    Start-Process -FilePath $TestExe | Out-Null
    Start-Sleep -Seconds 16
    $restartEdge = Find-AppEdge $ActiveHome
    $results.activated_restart_offline = [ordered]@{ pass=[bool]$restartEdge }
    Stop-TestApp $ActiveHome
    & icacls $RunDir /remove:d $identity /T /C | Out-Null
} finally {
    $env:PATH = $OriginalPath
    Remove-Item Env:MIXCUT_APP_HOME -ErrorAction SilentlyContinue
    $results | ConvertTo-Json -Depth 8 | Set-Content $ResultsPath -Encoding UTF8
    Write-Host "RESULTS=$ResultsPath"
}
