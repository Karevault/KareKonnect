#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -d ".venv" ]; then
  echo "Missing .venv. Run: ./scripts/bootstrap.sh" >&2
  exit 1
fi

source .venv/bin/activate

set -a
if [ -f ".env" ]; then
  # shellcheck disable=SC1091
  source .env
fi
set +a

python3 ./scripts/check_db.py

# Match Railway: trust proxy forwarded headers when developing behind HTTPS tunnels.
export FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-*}"
uvicorn app.main:app --reload --port 8000
