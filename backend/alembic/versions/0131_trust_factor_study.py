"""The pre-registered factor study's one result per spec version (ADR 0018 §8).

``trust_factor_study`` is append-only like the other evidence ledgers: on
Postgres a BEFORE UPDATE trigger reuses ``trust_reject_update()`` from
``0130_trust_evidence_ledgers``. SQLite gets the table only.

Revision ID: 0131_trust_factor_study
Revises: 0130_trust_evidence_ledgers
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0131_trust_factor_study"
down_revision = "0130_trust_evidence_ledgers"
branch_labels = None
depends_on = None

_TABLE = "trust_factor_study"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("spec_version", sa.Integer(), nullable=False),
            sa.Column("spec_json", sa.JSON(), nullable=False),
            sa.Column("decision", sa.String(16), nullable=False),
            sa.Column("oos_r2", sa.Float(), nullable=False),
            sa.Column("n_days", sa.Integer(), nullable=False),
            sa.Column("n_oos_days", sa.Integer(), nullable=False),
            sa.Column("detail_json", sa.JSON(), nullable=False),
            sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("user_id", "spec_version", name="uq_trust_factor_study_spec"),
        )
        op.create_index("ix_trust_factor_study_user_id", _TABLE, ["user_id"])

    if bind.dialect.name != "postgresql":
        return
    # get_bind() is already inside env.py's transaction: execute directly.
    bind.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_append_only ON {_TABLE}"))
    bind.execute(sa.text(
        f"CREATE TRIGGER trg_{_TABLE}_append_only BEFORE UPDATE ON {_TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION trust_reject_update()"
    ))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_append_only ON {_TABLE}"))
    op.drop_index("ix_trust_factor_study_user_id", table_name=_TABLE)
    op.drop_table(_TABLE)
