#!/usr/bin/env bash
# One-time setup on macOS or Linux: creates .venv, installs packages and a starter .env.
# Run from the project folder:  bash setup.sh
set -euo pipefail
cd "$(dirname "$0")"

python_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; }

if command -v uv >/dev/null 2>&1; then
  # uv downloads Python 3.12 by itself when the Mac does not have it.
  uv venv --python 3.12 .venv
  uv pip install --python .venv/bin/python -r requirements.txt
else
  PY=""
  for candidate in python3.13 python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && python_ok "$candidate"; then PY="$(command -v "$candidate")"; break; fi
  done
  if [ -z "$PY" ]; then
    echo "This app needs Python 3.12 or newer, which was not found." >&2
    echo "Easiest fix: install uv and run this script again:" >&2
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh     (or: brew install uv)" >&2
    echo "Or install Python 3.12 from https://www.python.org/downloads/macos/" >&2
    exit 1
  fi
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
fi

chmod +x scripts/run.sh setup.sh 2>/dev/null || true   # a ZIP download can drop the executable flag
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example (the Claude key can be added in the app instead)."
fi
mkdir -p data
echo
echo "Setup finished. Start the app with:  bash scripts/run.sh   then open http://127.0.0.1:8001"
echo "Demo with made-up records only:      .venv/bin/python scripts/ui_preview.py --reseed"
