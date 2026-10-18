param([ValidateSet(8001)][int]$Port = 8001)
$workspace = Split-Path -Parent $PSScriptRoot
$pythonRuntime = Join-Path (Split-Path -Parent $workspace) 'alina-fashion/.venv/Scripts/python.exe'
Set-Location -LiteralPath $workspace
& $pythonRuntime -m uvicorn app.main:app --host 127.0.0.1 --port $Port
