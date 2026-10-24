#Requires -Version 5.1
# One-time setup after downloading: creates .venv, installs packages and a starter .env.
# Run from the project folder:  powershell -ExecutionPolicy Bypass -File .\setup.ps1
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv venv --python 3.12 .venv
    uv pip install --python .venv\Scripts\python.exe -r requirements.txt
} else {
    $python = Get-Command py -ErrorAction SilentlyContinue
    if ($python) { & py -3.12 -m venv .venv } else { & python -m venv .venv }
    & .venv\Scripts\python.exe -m pip install --upgrade pip
    & .venv\Scripts\python.exe -m pip install -r requirements.txt
}

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host 'Created .env from .env.example (edit it only if you need to; the Claude key can be added in the app).'
}
New-Item -ItemType Directory -Force data | Out-Null
Write-Host ''
Write-Host 'Setup finished. Start the app with:  .\scripts\run.ps1   then open http://127.0.0.1:8001'
