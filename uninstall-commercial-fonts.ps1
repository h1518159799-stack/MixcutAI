$ErrorActionPreference = "Stop"
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
foreach ($Font in $Fonts) {
  Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows NT\CurrentVersion\Fonts" -Name ($Font.Name + " (TrueType)") -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath (Join-Path $UserFonts $Font.File) -Force -ErrorAction SilentlyContinue
}
Write-Host "Bundled commercial fonts removed for current user."
