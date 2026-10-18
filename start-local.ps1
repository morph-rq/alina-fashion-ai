$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:FASHION_DATABASE_URL = 'sqlite:///C:/Users/user/Documents/ChatGPT/alina-fashion-local/data/fashion_orders.db'
& 'C:\Users\user\Documents\ChatGPT\alina-fashion\.venv\Scripts\python.exe' -m uvicorn app.main:app --host 127.0.0.1 --port 8001
