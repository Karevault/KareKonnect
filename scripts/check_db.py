import os
import sys

from sqlalchemy import create_engine, text


def main() -> int:
    url = os.getenv("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 2

    engine = create_engine(url, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return 0
    except Exception as e:
        print(f"Database is not reachable: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

