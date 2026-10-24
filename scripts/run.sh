#!/usr/bin/env bash
# Start the app on http://127.0.0.1:8001 (macOS or Linux). Stop it with Ctrl+C.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  echo "Run ./setup.sh first." >&2
  exit 1
fi
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
