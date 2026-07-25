param([string]$OutputRoot = 'D:\MixcutAI_Backup')
$ErrorActionPreference = 'Stop'
$ProjectRoot = $PSScriptRoot
$stamp = Get-Date -Format 'yyyyMMdd-HHmm'
New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null

$base = "MixcutAI-source-$stamp"
$zip = Join-Path $OutputRoot "$base.zip"
$n = 1
while (Test-Path -LiteralPath $zip) {
  $base = "MixcutAI-source-$stamp-{0:D2}" -f $n; $n++
  $zip = Join-Path $OutputRoot "$base.zip"
}
$stageRoot = Join-Path $OutputRoot ".staging-$base"
$stage = Join-Path $stageRoot 'ai_mixcut_desktop'

try {
  if (Test-Path "$ProjectRoot\data\mixcut.db") {
    python "$ProjectRoot\backup_tools\sanitize_database.py" --source "$ProjectRoot\data\mixcut.db" --output "$ProjectRoot\backup_data_sanitized\mixcut_sanitized.db" --schema "$ProjectRoot\backup_data_sanitized\schema.sql"
    if ($LASTEXITCODE -ne 0) { throw 'database sanitization failed' }
  }
  New-Item -ItemType Directory -Force -Path $stage | Out-Null
  $excludedDirs = @('node_modules','.venv','venv','__pycache__','.pytest_cache','.vite','dist','build','release','admin_release','outputs','data','browser_profiles','playwright-browsers')
  $files = Get-ChildItem -LiteralPath $ProjectRoot -File -Recurse -Force | Where-Object {
    $rel = $_.FullName.Substring($ProjectRoot.Length).TrimStart('\')
    $parts = $rel -split '[\\/]'
    if ($parts | Where-Object { $excludedDirs -contains $_ }) { return $false }
    if ($_.Length -gt 50MB) { return $false }
    if ($_.Extension -in @('.log','.tmp','.temp','.pyc','.pyo')) { return $false }
    if ($_.Name -eq '.env' -or ($_.Name -like '.env.*' -and $_.Name -ne '.env.example')) { return $false }
    if ($_.Name -match '(?i)(license\.dat|cookie|storage_state|access_token|refresh_token)') { return $false }
    if ($_.Name -match '(?i)(private|secret).*[.](pem|key)$' -or $_.Name -match '(?i)[.](p12|pfx)$') { return $false }
    if ($_.Extension -in @('.db','.sqlite','.sqlite3') -and $parts[0] -ne 'backup_data_sanitized') { return $false }
    if ($parts[0] -eq 'qa' -and $_.Name -match '^(result|report|screenshot|snapshot|runtime).*[.](json|html|png|jpg)$') { return $false }
    return $true
  }
  foreach ($f in $files) {
    $rel = $f.FullName.Substring($ProjectRoot.Length).TrimStart('\')
    $dest = Join-Path $stage $rel
    New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
    Copy-Item -LiteralPath $f.FullName -Destination $dest -Force
  }

  $manifest = Get-ChildItem -LiteralPath $stage -File -Recurse -Force | ForEach-Object { $_.FullName.Substring($stage.Length).TrimStart('\') } | Sort-Object
  $manifest | Set-Content -LiteralPath (Join-Path $stage 'BACKUP_FILE_MANIFEST.txt') -Encoding UTF8

  $bad = @()
  $scanFiles = Get-ChildItem -LiteralPath $stage -File -Recurse -Force | Where-Object { $_.Length -lt 5MB -and $_.Extension -match '^\.(py|js|jsx|json|md|txt|toml|ps1|env|yaml|yml|ini|cfg|pem)$' }
  foreach ($f in $scanFiles) {
    $text = Get-Content -LiteralPath $f.FullName -Raw -ErrorAction SilentlyContinue
    if ($text -match '-----BEGIN (RSA |EC |OPENSSH |ED25519 )?PRIVATE KEY-----') { $bad += $f.FullName }
    # Only flag a likely hard-coded quoted secret. Environment lookups and empty examples are allowed.
    if ($text -match '(?im)(API_KEY|CLIENT_SECRET|ACCESS_TOKEN|REFRESH_TOKEN)\s*[=:]\s*["''][A-Za-z0-9_\-\.]{16,}["'']') { $bad += $f.FullName }
  }
  if ($bad.Count) { throw "sensitive content check failed: $($bad -join ', ')" }

  Add-Type -AssemblyName System.IO.Compression.FileSystem
  [System.IO.Compression.ZipFile]::CreateFromDirectory($stageRoot, $zip, [System.IO.Compression.CompressionLevel]::Optimal, $false)
  $hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
  $sha = Join-Path $OutputRoot "$base.sha256.txt"
  "$hash  $([IO.Path]::GetFileName($zip))" | Set-Content -LiteralPath $sha -Encoding ascii
  Write-Output "ZIP=$zip"
  Write-Output "SHA256=$hash"
  Write-Output "SIZE=$((Get-Item $zip).Length)"
} finally {
  if (Test-Path -LiteralPath $stageRoot) { Remove-Item -LiteralPath $stageRoot -Recurse -Force }
}
