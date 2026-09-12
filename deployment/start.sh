#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# Index initialization is an explicit release operation, never a cold-start
# provider call. This check refuses a silently downgraded or stale deployment.
uv run --no-sync alembic upgrade head
uv run --no-sync python deployment/check_index.py
exec uv run --no-sync uvicorn emer.api.app:app --host 0.0.0.0 --port "${PORT:-8017}" --workers 1 --no-access-log
