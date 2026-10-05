"""Phase 6: FinAgent, pgvector embeddings, LLM infrastructure

Revision ID: 0020_phase6_pgvector
Revises: 0019_phase5_verification
Create Date: 2026-05-24
"""

from alembic import op
import sqlalchemy as sa


revision = "0020_phase6_pgvector"
down_revision = "0019_phase5_verification"
branch_labels = None
depends_on = None


def _index_names(table_name):
    """Return the set of existing index names on ``table_name`` (empty if the table is gone)."""
    inspector = sa.inspect(op.get_bind())
    try:
        return {idx["name"] for idx in inspector.get_indexes(table_name)}
    except sa.exc.NoSuchTableError:
        return set()


def safe_create_index(index_name, table_name, columns, unique=False, **kwargs):
    """Create an index only if it does not already exist (idempotent / duplicate-safe)."""
    resolved = op.f(index_name)
    if resolved not in _index_names(table_name):
        op.create_index(resolved, table_name, columns, unique=unique, **kwargs)


def safe_drop_index(index_name, table_name=None, **kwargs):
    """Drop an index only if it exists (idempotent / safe on re-run)."""
    resolved = op.f(index_name)
    if table_name is not None and resolved in _index_names(table_name):
        op.drop_index(resolved, table_name=table_name, **kwargs)
    elif table_name is None:
        op.drop_index(resolved, **kwargs)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Enable pgvector extension
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))

    # Create embeddings table with pgvector
    if not inspector.has_table("embeddings"):
        op.create_table(
            "embeddings",
            sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True),
            sa.Column("item_id", sa.String(length=255), nullable=False),
            sa.Column("item_type", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("meta_json", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        safe_create_index("ix_embeddings_item_type_id", "embeddings", ["item_type", "item_id"])
        safe_create_index("ix_embeddings_created_at", "embeddings", [sa.desc("created_at")])

        # Add pgvector column for PostgreSQL (SQLite doesn't support pgvector)
        if bind.dialect.name == "postgresql":
            bind.execute(sa.text("ALTER TABLE embeddings ADD COLUMN embedding vector(1024)"))
            # Create ivfflat index for similarity search
            bind.execute(sa.text("""
                CREATE INDEX IF NOT EXISTS ix_embeddings_vector ON embeddings
                USING ivfflat (embedding vector_l2_ops)
                WITH (lists = 100)
            """))

    # Create finagent_runs table (non-hypertable, point-in-time records)
    if not inspector.has_table("finagent_runs"):
        op.create_table(
            "finagent_runs",
            sa.Column("id", sa.UUID(), nullable=False, server_default=sa.func.gen_random_uuid()),
            sa.Column("agent", sa.String(length=64), nullable=False),  # budget, substitution, explainer
            sa.Column("query", sa.Text(), nullable=False),
            sa.Column("result_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.PrimaryKeyConstraint("id"),
        )
        safe_create_index("ix_finagent_runs_created_at", "finagent_runs", [sa.desc("created_at")])
        safe_create_index("ix_finagent_runs_agent", "finagent_runs", ["agent"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("finagent_runs"):
        op.drop_table("finagent_runs")

    if inspector.has_table("embeddings"):
        op.drop_table("embeddings")

    if bind.dialect.name == "postgresql":
        try:
            bind.execute(sa.text("DROP EXTENSION IF EXISTS vector"))
        except Exception:
            pass
