#!/bin/sh
set -eu
cd /app
export PYTHONPATH="${PYTHONPATH:-/app}"
python scripts/render_enroll_hive.py
exec python -m uvicorn hive.main:app --host 0.0.0.0 --port "${PORT:-10000}"
