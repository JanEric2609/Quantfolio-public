"""Seed a throwaway database for the browser smoke test (frontend/e2e).

Creates one admin user on an otherwise empty database, so every page is
opened in the state a new install starts in. Refuses to touch anything but
a SQLite file, so it can never run against production.

    DATABASE_URL=sqlite:////tmp/smoke.db .venv/bin/alembic upgrade head
    DATABASE_URL=sqlite:////tmp/smoke.db SMOKE_PASSWORD=... .venv/bin/python scripts/seed_smoke_db.py
"""
from __future__ import annotations

import os
import pathlib
import sys

_BACKEND_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def main() -> int:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("sqlite:///"):
        print(f"refusing to seed {url!r}: the smoke seed only writes to a SQLite file", file=sys.stderr)
        return 2
    username = os.environ.get("SMOKE_USER", "smoke")
    password = os.environ.get("SMOKE_PASSWORD", "")
    if len(password) < 12:
        print("SMOKE_PASSWORD must be at least 12 characters", file=sys.stderr)
        return 2

    from app.foundation.core.db import SessionLocal
    from app.foundation.core.security import hash_password
    from app.foundation.models.entities import User

    with SessionLocal() as db:
        user = db.query(User).filter(User.username == username).one_or_none()
        if user is None:
            user = User(username=username, password_hash=hash_password(password), role="admin")
            db.add(user)
        else:
            user.password_hash = hash_password(password)
        db.commit()
    print(f"seeded admin {username!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
