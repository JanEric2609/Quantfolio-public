"""Drop orphaned Plan/Knowledge tables (aspects, knowledge_items).

The Plan (goals/aspects) and Knowledge tabs were removed in the advisor-loop
PR1 cleanup. The ``goals`` table is intentionally KEPT — the Quant Lab goals
view (``app/api/quant/goals.py`` + ``services/goal_optimizer.py``) still reads
it. Only tables proven orphaned by grep are dropped here:

- ``aspects``        (entity ``Aspect``, only reader was ``api/aspects.py``)
- ``knowledge_items`` (entity ``KnowledgeItem``, readers were ``api/knowledge.py``
  and the dead ``index_vault_to_knowledge_items`` helper)

``downgrade()`` faithfully recreates both tables from the deleted entity
schemas, so the migration is reversible (data is not restored).

Revision ID: 0079_drop_plan_knowledge
Revises: 0078_llm_decision_verdict
"""

from alembic import op
import sqlalchemy as sa

revision = "0079_drop_plan_knowledge"
down_revision = "0078_llm_decision_verdict"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("aspects"):
        op.drop_table("aspects")
    if inspector.has_table("knowledge_items"):
        op.drop_table("knowledge_items")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("aspects"):
        op.create_table(
            "aspects",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    if not inspector.has_table("knowledge_items"):
        op.create_table(
            "knowledge_items",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("title", sa.String(160), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("category", sa.String(80), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
