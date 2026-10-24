#!/usr/bin/env bash
# One-time setup on macOS or Linux: creates .venv, installs packages and a starter .env.
# Run from the project folder:  ./setup.sh
set -euo pipefail
cd "$(dirname "$0")"

if command -v uv >/dev/null 2>&1; then
  uv venv --python 3.12 .venv
  uv pip install --python .venv/bin/python -r requirements.txt
else
  PY="$(command -v python3.12 || command -v python3 || true)"
  if [ -z "$PY" ]; then
    echo "Python 3.12 is not installed. Install it from python.org or with: brew install python@3.12" >&2
    exit 1
  fi
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example (the Claude key can be added in the app instead)."
fi
mkdir -p data
echo
echo "Setup finished. Start the app with:  ./scripts/run.sh   then open http://127.0.0.1:8001"
echo "Demo with made-up records only:      .venv/bin/python scripts/ui_preview.py --reseed"
