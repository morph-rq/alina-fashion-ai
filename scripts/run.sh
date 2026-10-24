#!/usr/bin/env bash
# Start the app on http://127.0.0.1:8001 (macOS or Linux). Stop it with Ctrl+C.
# Another port, e.g. when 8001 is busy:  PORT=8002 bash scripts/run.sh
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  echo "Run: bash setup.sh   first." >&2
  exit 1
fi
PORT="${PORT:-8001}"
echo "Open http://127.0.0.1:${PORT}   (Ctrl+C stops the app)"
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
