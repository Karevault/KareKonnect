#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

./scripts/bootstrap.sh
./scripts/start_db.sh || true

echo "Starting API on :8000"
./scripts/start_api.sh
