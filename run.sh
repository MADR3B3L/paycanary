#!/usr/bin/env bash
# One-command offline demo, no Docker, no keys:  ./run.sh   (add "dev" to also install test deps, "test" to run the tests)
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
MODE="${1:-demo}"
if [ ! -x .venv/bin/python ]; then "$PY" -m venv .venv; fi
REQ=requirements.txt; [ "$MODE" = "dev" ] || [ "$MODE" = "test" ] && REQ=requirements-dev.txt
.venv/bin/pip install -q -r "$REQ"
if [ "$MODE" = "test" ]; then exec .venv/bin/python -m unittest discover -s tests -t .; fi
export PAYCANARY_DEMO=1 PAYCANARY_DEMO_OFFLINE=1
TOKEN=$(.venv/bin/python -c "from pathlib import Path; from app import auth; print(auth.load_tokens(Path('.'))[0])")
printf '\n  PayCanary offline demo: http://127.0.0.1:%s/\n  Operator token (paste into the dashboard): %s\n\n' "${PORT:-8000}" "$TOKEN"
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8000}"
