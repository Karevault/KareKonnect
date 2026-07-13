#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -d ".venv" ]; then
  echo "Missing .venv. Run: ./scripts/bootstrap.sh" >&2
  exit 1
fi

source .venv/bin/activate
exec python3 ./scripts/create_admin.py "$@"
