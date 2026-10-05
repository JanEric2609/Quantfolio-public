"""Delete all users from quantfolio.db so you can re-register.

Usage (run from the repo root or the backend/ directory):
    python3 backend/scripts/reset_users.py
    # or
    cd backend && .venv/bin/python scripts/reset_users.py
"""
import os
import pathlib
import sqlite3
import sys

# Resolve the DB path: walk up from this script until we find quantfolio.db
_here = pathlib.Path(__file__).resolve().parent
for _candidate in (_here, _here.parent, _here.parent.parent):
    _db = _candidate / "quantfolio.db"
    if _db.exists():
        break
else:
    _db_env = os.getenv("DATABASE_URL", "")
    if _db_env.startswith("sqlite:///"):
        _db = pathlib.Path(_db_env.removeprefix("sqlite:///"))
    else:
        print("quantfolio.db not found. Set DATABASE_URL or run from the repo root.")
        sys.exit(1)

conn = sqlite3.connect(str(_db))
count = conn.execute("DELETE FROM users").rowcount
conn.commit()
conn.close()
print(f"Deleted {count} user(s) from {_db}.")
print("You can now re-register at the login page.")
