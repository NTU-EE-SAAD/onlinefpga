#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -x .venv/bin/python ]]; then
  echo "請先依 README 建立 .venv 並安裝 requirements.txt。" >&2
  exit 1
fi
mkdir -p instance
chmod 700 instance
if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi
export PYTHONUNBUFFERED=1
.venv/bin/flask --app portal init-db
.venv/bin/flask --app portal scheduler &
portal_worker_pid=$!
cleanup() {
  kill "$portal_worker_pid" 2>/dev/null || true
  if [[ -n "${portal_web_pid:-}" ]]; then kill "$portal_web_pid" 2>/dev/null || true; fi
  wait || true
}
trap cleanup EXIT INT TERM
.venv/bin/gunicorn --bind "${PORTAL_BIND:-0.0.0.0:8000}" --workers 2 --worker-class gthread --threads 32 --timeout 90 'portal:create_app()' &
portal_web_pid=$!
wait -n "$portal_worker_pid" "$portal_web_pid"
