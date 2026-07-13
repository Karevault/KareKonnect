#!/usr/bin/env python3
"""
Create or update an admin user (bcrypt password hash stored in Postgres).

Usage:
  python scripts/create_admin.py admin 'your-password'
  python scripts/create_admin.py admin 'new-password' --update
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:
    pass

from sqlalchemy import select  # noqa: E402

from app.database import session_scope  # noqa: E402
from app.db import init_db  # noqa: E402
from app.models import AdminUser  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.util import new_id, now_iso  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or update Kareconnect admin user")
    parser.add_argument("username", help="Login username (case-sensitive)")
    parser.add_argument("password", help="Plain password (will be hashed)")
    parser.add_argument("--update", action="store_true", help="Update password if user already exists")
    args = parser.parse_args()

    init_db()

    username = args.username.strip()
    if not username or not args.password:
        print("Username and password are required.", file=sys.stderr)
        return 2

    now = now_iso()
    pw_hash = hash_password(args.password)

    with session_scope() as s:
        existing = s.execute(select(AdminUser).where(AdminUser.username == username)).scalar_one_or_none()
        if existing:
            if not args.update:
                print(f"User '{username}' already exists. Re-run with --update to change password.", file=sys.stderr)
                return 1
            existing.password_hash = pw_hash
            existing.updated_at = now
            print(f"Updated password for admin user '{username}'.")
            return 0

        s.add(
            AdminUser(
                id=new_id("adm"),
                username=username,
                password_hash=pw_hash,
                is_active=True,
                is_superuser=True,
                created_at=now,
                updated_at=now,
            )
        )
        print(f"Created admin user '{username}'.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
