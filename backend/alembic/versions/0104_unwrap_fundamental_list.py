"""Unwrap legacy list data_json payloads in fundamentals table.

Revision ID: 0104_unwrap_fundamental_list
Revises: 0103_quant_ml_model_ticker
"""
import json
from alembic import op
import sqlalchemy as sa

revision = "0104_unwrap_fundamental_list"
down_revision = "0103_quant_ml_model_ticker"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("fundamentals"):
        rows = bind.execute(
            sa.text("SELECT ticker, data_json FROM fundamentals WHERE data_json LIKE '[%'")
        ).fetchall()

        for row in rows:
            ticker, raw_json = row[0], row[1]
            try:
                parsed = json.loads(raw_json)
                if isinstance(parsed, list):
                    unwrapped = parsed[0] if parsed and isinstance(parsed[0], dict) else {}
                    bind.execute(
                        sa.text("UPDATE fundamentals SET data_json = :clean_json WHERE ticker = :t"),
                        {"clean_json": json.dumps(unwrapped), "t": ticker},
                    )
            except Exception:
                pass


def downgrade() -> None:
    pass
