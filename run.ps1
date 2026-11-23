# Windows one-liner setup + run:  powershell -ExecutionPolicy Bypass -File run.ps1
if (-not (Test-Path .venv)) { python -m venv .venv }
.\.venv\Scripts\python -m pip install -q -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
Write-Host "AP Autopilot -> http://localhost:8000"
.\.venv\Scripts\python -m uvicorn app.main:app --port 8000
