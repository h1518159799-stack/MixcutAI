$ErrorActionPreference = "Stop"
$RuntimePython = "C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (Test-Path $RuntimePython) {
    & $RuntimePython (Join-Path $AppDir "app.py")
} else {
    py -3 (Join-Path $AppDir "app.py")
}
