$ErrorActionPreference = "Stop"
$Server = "root@47.107.104.106"
$Identity = Join-Path $HOME ".ssh\mixcut_aliyun_ed25519"
$Desktop = [Environment]::GetFolderPath("Desktop")

if (-not (Test-Path $Identity)) {
    Write-Host "找不到服务器管理密钥：$Identity" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}

Write-Host "Mixcut 在线卡密生成器" -ForegroundColor Cyan
Write-Host "1. 月卡（30天）"
Write-Host "2. 年卡（365天）"
Write-Host "3. 永久授权"
$choice = Read-Host "请选择 1/2/3"
$kind = switch ($choice) {
    "1" { "month" }
    "2" { "year" }
    "3" { "permanent" }
    default { throw "选择无效" }
}
$count = [int](Read-Host "生成数量")
if ($count -lt 1 -or $count -gt 1000) { throw "数量必须在 1 到 1000 之间" }

$remote = "/opt/mixcut/venv/bin/python /opt/mixcut/server/mixcut_admin.py create --type $kind --count $count"
$keys = & ssh.exe -i $Identity -o BatchMode=yes $Server $remote
if ($LASTEXITCODE -ne 0 -or -not $keys) { throw "服务器生成卡密失败" }

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$output = Join-Path $Desktop "Mixcut-$kind-keys-$stamp.txt"
$keys | Set-Content -Path $output -Encoding UTF8
Write-Host "生成成功：$output" -ForegroundColor Green
Start-Process notepad.exe $output
Read-Host "按回车退出"

