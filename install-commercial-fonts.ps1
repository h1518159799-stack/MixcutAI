$ErrorActionPreference = "Stop"
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Bundle = Join-Path $AppDir "data\fonts"
$UserFonts = Join-Path $env:LOCALAPPDATA "Microsoft\Windows\Fonts"
$Fonts = @(
  @{ Name="Noto Sans SC"; File="NotoSansSC-Variable.ttf" },
  @{ Name="Noto Serif SC"; File="NotoSerifSC-Variable.ttf" },
  @{ Name="Ma Shan Zheng"; File="MaShanZheng-Regular.ttf" },
  @{ Name="Long Cang"; File="LongCang-Regular.ttf" }
  @{ Name="Smiley Sans Oblique"; File="SmileySans-Oblique.ttf" }
  @{ Name="WDXL Lubrifont SC"; File="WDXLLubrifontSC-Regular.ttf" }
  @{ Name="ZCOOL KuaiLe"; File="ZCOOLKuaiLe-Regular.ttf" }
  @{ Name="ZCOOL QingKe HuangYou"; File="ZCOOLQingKeHuangYou-Regular.ttf" }
  @{ Name="ZCOOL XiaoWei"; File="ZCOOLXiaoWei-Regular.ttf" }
  @{ Name="LXGW WenKai GB Screen"; File="LXGWWenKaiGBScreen.ttf" }
)
New-Item -ItemType Directory -Force -Path $UserFonts | Out-Null
New-Item -Path "HKCU:\Software\Microsoft\Windows NT\CurrentVersion\Fonts" -Force | Out-Null
foreach ($Font in $Fonts) {
  $Source = Join-Path $Bundle $Font.File
  if (-not (Test-Path $Source)) { throw "Missing bundled font: $Source" }
  $Destination = Join-Path $UserFonts $Font.File
  $NeedsCopy = -not (Test-Path -LiteralPath $Destination)
  if (-not $NeedsCopy) {
    $NeedsCopy = (Get-Item -LiteralPath $Source).Length -ne (Get-Item -LiteralPath $Destination).Length
  }
  if ($NeedsCopy) { Copy-Item -LiteralPath $Source -Destination $Destination -Force }
  New-ItemProperty -Path "HKCU:\Software\Microsoft\Windows NT\CurrentVersion\Fonts" -Name ($Font.Name + " (TrueType)") -Value $Destination -PropertyType String -Force | Out-Null
}
Write-Host "Commercial fonts installed for current user."
