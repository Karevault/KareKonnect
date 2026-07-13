#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "Stopping API (port 8000)..."
lsof -tiTCP:8000 -sTCP:LISTEN | xargs kill -9 2>/dev/null || true

echo "Restarting Postgres (Homebrew)..."
if command -v brew >/dev/null 2>&1; then
  if brew list --versions postgresql@16 >/dev/null 2>&1; then
    brew services restart postgresql@16
  elif brew list --versions postgresql >/dev/null 2>&1; then
    brew services restart postgresql
  else
    echo "No Homebrew PostgreSQL formula found; skipping DB restart."
  fi
else
  echo "Homebrew not found; skipping DB restart."
fi

sleep 2

echo "Ensuring DB role/database..."
./scripts/start_db.sh

echo "Starting API..."
exec ./scripts/start_api.sh
