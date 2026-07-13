#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew not found. Stop Postgres using your system service manager." >&2
  exit 1
fi

if brew list --versions postgresql@16 >/dev/null 2>&1; then
  brew services stop postgresql@16 >/dev/null
  echo "Postgres stopped (postgresql@16)."
  exit 0
fi

if brew list --versions postgresql >/dev/null 2>&1; then
  brew services stop postgresql >/dev/null
  echo "Postgres stopped (postgresql)."
  exit 0
fi

echo "PostgreSQL isn't installed via Homebrew, nothing to stop here."
