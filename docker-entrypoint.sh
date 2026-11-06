#!/bin/sh
# Creates (or reads) the operator token, prints it so `docker compose up` shows how to log in, then starts the server.
python - <<'PY'
from pathlib import Path
from app import auth
op, _ = auth.load_tokens(Path("."))
print("\n  PayCanary demo is starting at http://localhost:8000/\n  Operator token (paste it into the dashboard): " + op + "\n", flush=True)
PY
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
