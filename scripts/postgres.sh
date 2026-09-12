#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# A clean checkout may share an already running local database. Reuse it instead
# of creating another cluster and attempting to bind the same port.
if ! pg_isready -h 127.0.0.1 -p 55432 >/dev/null 2>&1; then
  mkdir -p .local
  if [ ! -f .local/postgres/PG_VERSION ]; then
    initdb -D .local/postgres -A trust --no-locale --encoding=UTF8 >/dev/null
  fi
  if ! pg_ctl -D .local/postgres status >/dev/null 2>&1; then
    pg_ctl -D .local/postgres -l .local/postgres.log -o "-p 55432 -h 127.0.0.1 -k /tmp" start
  fi
fi
if ! psql -h 127.0.0.1 -p 55432 -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='emer'" | tr -d ' ' | rg -q '^1$'; then
  createdb -h 127.0.0.1 -p 55432 emer
fi
