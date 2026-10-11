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
# Same check the app uses: an API key in the env or .env, or an `ant auth login` profile.
has_key() { .venv/bin/python -c "from app.config import load_dotenv; load_dotenv(); from app.services.claude_detector import is_configured; raise SystemExit(0 if is_configured() else 1)"; }
export DRA_NO_LOGIN="${DRA_NO_LOGIN:-1}"

if has_key; then
  .venv/bin/python -m app.cli seed-demo --if-empty
else
  echo "⚠  No Claude API access (ANTHROPIC_API_KEY in .env, or \`ant auth login\`) – Claude vision is off; seeding synthetic boxes instead."
  .venv/bin/python -m app.cli seed-demo --if-empty --detector demo
fi

if lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Something is already running on port 8000 – open http://127.0.0.1:8000 or stop it first."; exit 1
fi
(sleep 2; command -v open >/dev/null && open "http://127.0.0.1:8000/") &
exec .venv/bin/python server.py
