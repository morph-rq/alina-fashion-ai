param([ValidateSet(8001)][int]$Port = 8001)
$workspace = Split-Path -Parent $PSScriptRoot
# This project's own .venv (from setup.ps1); the sibling operational copy's runtime only as a fallback.
$pythonRuntime = Join-Path $workspace '.venv/Scripts/python.exe'
if (-not (Test-Path $pythonRuntime)) { $pythonRuntime = Join-Path (Split-Path -Parent $workspace) 'alina-fashion/.venv/Scripts/python.exe' }
if (-not (Test-Path $pythonRuntime)) { throw 'Run .\setup.ps1 first.' }
Set-Location -LiteralPath $workspace
& $pythonRuntime -m uvicorn app.main:app --host 127.0.0.1 --port $Port
