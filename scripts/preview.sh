#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

port="${PORT:-4197}"
if ! [[ "$port" =~ ^[1-9][0-9]{3,4}$ ]] || (( port < 1024 || port > 65535 )); then
  echo "PORT must be a local port between 1024 and 65535." >&2
  exit 1
fi

export APP_ORIGIN="http://localhost:$port"
export COOKIE_SECURE=false
npm --prefix apps/web run build
uv run --no-sync emer ingest --if-missing --embeddings --manifest config/corpus.json
uv run --no-sync python deployment/check_index.py
echo "EMER preview: $APP_ORIGIN (keep this terminal open)"
exec uv run --no-sync uvicorn emer.api.app:app --host 127.0.0.1 --port "$port" --no-access-log --log-level warning
