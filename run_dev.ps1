$ErrorActionPreference = "Stop"
if (-not (Test-Path ".venv\Scripts\python.exe")) { throw "Run setup_dev.ps1 first" }
& ".\.venv\Scripts\python.exe" app.py
