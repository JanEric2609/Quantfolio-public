"""enable TimescaleDB extension

Revision ID: 0014_timescale_enable
Revises: 0013_phase9_entities
Create Date: 2026-05-24
"""

from alembic import op
import sqlalchemy as sa


revision = "0014_timescale_enable"
down_revision = "0013_phase9_entities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Enable TimescaleDB extension (PostgreSQL only)
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("CREATE EXTENSION IF NOT EXISTS timescaledb"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Disable TimescaleDB extension (PostgreSQL only)
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("DROP EXTENSION IF EXISTS timescaledb CASCADE"))
