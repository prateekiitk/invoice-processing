#!/usr/bin/env sh
# macOS / Linux setup + run:  sh run.sh
set -e
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
echo "AP Autopilot -> http://localhost:8000"
exec uvicorn app.main:app --port 8000
