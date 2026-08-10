$ErrorActionPreference = "Stop"
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimePython = [string]$env:MIXCUT_PYTHON
if (-not $RuntimePython -or -not (Test-Path -LiteralPath $RuntimePython)) {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($PythonCommand) { $RuntimePython = $PythonCommand.Source }
}
if (-not $RuntimePython) {
    $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($PyLauncher) {
        & $PyLauncher.Source -3 (Join-Path $AppDir "commercial_launcher.py")
        exit $LASTEXITCODE
    }
    throw "Python 未安装。开发源码请先安装 Python；正式用户请运行 MixcutAI.exe。"
}
& $RuntimePython (Join-Path $AppDir "commercial_launcher.py")
