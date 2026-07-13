#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

set -a
if [ -f ".env" ]; then
  # shellcheck disable=SC1091
  source .env
fi
set +a

if ! command -v psql >/dev/null 2>&1; then
  echo "psql is required (PostgreSQL client not found)." >&2
  exit 1
fi

if ! command -v pg_isready >/dev/null 2>&1; then
  echo "pg_isready not found. Ensure PostgreSQL is installed." >&2
  exit 1
fi

if ! pg_isready >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    if brew list --versions postgresql@16 >/dev/null 2>&1; then
      brew services start postgresql@16 >/dev/null
    elif brew list --versions postgresql >/dev/null 2>&1; then
      brew services start postgresql >/dev/null
    else
      echo "PostgreSQL is not installed via Homebrew. Install it or start your local Postgres, then re-run." >&2
      exit 1
    fi
  else
    echo "Postgres is not running, and Homebrew isn't available to start it. Start Postgres, then re-run." >&2
    exit 1
  fi
fi

for _ in $(seq 1 30); do
  if pg_isready >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! pg_isready >/dev/null 2>&1; then
  echo "Postgres did not become ready on localhost:5432." >&2
  exit 1
fi

DB_NAME="${HC_DB_NAME:-healthconnect}"
DB_USER="${HC_DB_USER:-healthconnect}"
DB_PASS="${HC_DB_PASS:-healthconnect}"
DB_HOST="${HC_DB_HOST:-localhost}"
DB_PORT="${HC_DB_PORT:-5432}"

export PGPASSWORD="${PGPASSWORD:-}"

echo "Ensuring role/database exist..."
psql -v ON_ERROR_STOP=1 -h "$DB_HOST" -p "$DB_PORT" -d postgres <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${DB_USER}') THEN
    CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASS}';
  END IF;
END
\$\$;
SELECT 1 FROM pg_database WHERE datname='${DB_NAME}';
SQL

if ! psql -h "$DB_HOST" -p "$DB_PORT" -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; then
  createdb -h "$DB_HOST" -p "$DB_PORT" -O "$DB_USER" "$DB_NAME"
fi

echo "Postgres ready. DATABASE_URL should point to:"
echo "postgresql+psycopg2://${DB_USER}:${DB_PASS}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
