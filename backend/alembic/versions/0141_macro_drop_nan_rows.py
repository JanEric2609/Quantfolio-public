"""Delete the NaN rows FRED holidays wrote into macro_indicators (prod check 2026-10-26).

FRED reports a holiday as "."; the ingester stored it as NaN (BAA10Y 30,
T10Y2Y 29, VIXCLS 7 rows by 2026-10-26). It now skips them. Only Postgres
stores a numeric NaN, so the delete runs there alone.

Revision ID: 0141_macro_drop_nan_rows
Revises: 0140_plan_bands
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0141_macro_drop_nan_rows"
down_revision = "0140_plan_bands"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql" or not sa.inspect(bind).has_table("macro_indicators"):
        return
    bind.execute(sa.text("DELETE FROM macro_indicators WHERE value = 'NaN'::numeric"))


def downgrade() -> None:
    # The deleted rows held no observation; there is nothing to restore.
    pass
