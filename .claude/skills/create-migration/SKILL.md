---
name: create-migration
description: Create a new Alembic migration following Quantfolio project conventions (defensive guards, chained revisions, ≤32-char IDs)
---

Generate a new Alembic migration for: {{what}}

## Step 1 — Determine the next revision ID

Run this to find the latest revision:
```bash
cd backend && ls alembic/versions/*.py | sort | tail -1
```

Name the new revision `NNNN_short_slug` where NNNN is the next number and the full string is ≤32 characters (e.g. `0024_add_user_prefs`).

## Step 2 — Create the migration file

Create `backend/alembic/versions/<revision_id>.py` with this exact pattern:

```python
"""<one-line description>

Revision ID: <revision_id>
Revises: <previous_revision_id>
Create Date: <today>
"""
from alembic import op
import sqlalchemy as sa

revision = "<revision_id>"
down_revision = "<previous_revision_id>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("my_table"):
        op.create_table(
            "my_table",
            sa.Column("id", sa.String(36), primary_key=True),
            # ... columns
        )


def downgrade() -> None:
    op.drop_table("my_table")
```

## Rules (never skip these)

1. **Revision ID ≤32 chars** — `alembic_version.version_num` is `VARCHAR(32)`. Longer IDs silently break the chain.
2. **Always guard with `has_table`** — migrations must be idempotent so they can be run on SQLite for testing.
3. **Set `down_revision`** to the previous migration's `revision` value — verify by reading that file.
4. **Use `uuid_pk()` / `now_utc()` helpers** for primary keys and timestamps (defined in `app/foundation/models/entities/_core.py`, re-exported by the `entities` package).
5. **Foreign keys** use `ondelete="CASCADE"`.

## Step 3 — Validate

```bash
cd backend && DATABASE_URL="sqlite:///test.db" .venv/bin/alembic upgrade head
```

This runs the full chain on a fresh SQLite DB and catches broken guards or chain gaps before commit.
