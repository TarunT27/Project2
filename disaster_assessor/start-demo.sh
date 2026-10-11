#!/usr/bin/env bash
# One-command judge demo: sets up Python, seeds the Hurricane Ian incident, starts the server, opens the browser.
#   ./start-demo.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Creating Python environment…"
  if command -v uv >/dev/null 2>&1; then uv venv --python 3.12 .venv
  elif command -v python3.12 >/dev/null 2>&1; then python3.12 -m venv .venv
  elif command -v python3.11 >/dev/null 2>&1; then python3.11 -m venv .venv
  else echo "Python 3.11+ is required (install it, or install uv: https://docs.astral.sh/uv/)"; exit 1; fi
fi
if ! .venv/bin/python -c "import fastapi, anthropic, pyproj, fpdf, shapefile" 2>/dev/null; then
  echo "Installing dependencies…"
  if command -v uv >/dev/null 2>&1; then uv pip install --python .venv/bin/python -q -r requirements-demo.txt
  else .venv/bin/python -m pip install -q -r requirements-demo.txt; fi
fi

[ -f .env ] || cp .env.example .env
has_key() { [ -n "${ANTHROPIC_API_KEY:-}" ] || grep -Eq '^ANTHROPIC_API_KEY=.{8,}' .env; }
export DRA_NO_LOGIN="${DRA_NO_LOGIN:-1}"

if has_key; then
  .venv/bin/python -m app.cli seed-demo --if-empty
else
  echo "⚠  ANTHROPIC_API_KEY is empty in disaster_assessor/.env – Claude vision is off; seeding synthetic boxes instead."
  .venv/bin/python -m app.cli seed-demo --if-empty --detector demo
fi

if lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Something is already running on port 8000 – open http://127.0.0.1:8000 or stop it first."; exit 1
fi
(sleep 2; command -v open >/dev/null && open "http://127.0.0.1:8000/") &
exec .venv/bin/python server.py
