"""Tests for migration 0041: shared_memory on alphacrafter_job_runs, regime_applicability on factors_library."""
from __future__ import annotations

from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base


def _memory_db():
    import app.foundation.models.entities  # noqa: F401
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


def test_alphacrafter_job_run_has_shared_memory_column():
    engine = _memory_db()
    inspector = inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("alphacrafter_job_runs")}
    assert "shared_memory" in cols


def test_factors_library_has_regime_applicability_column():
    engine = _memory_db()
    inspector = inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("factors_library")}
    assert "regime_applicability" in cols


def test_migration_file_exists():
    from pathlib import Path

    migration_path = Path("alembic/versions/0041_shared_memory.py")
    assert migration_path.exists(), f"Migration file not found: {migration_path}"


def test_migration_has_guard_and_down_revision():
    from pathlib import Path

    content = Path("alembic/versions/0041_shared_memory.py").read_text()
    assert 'down_revision = "0040_recommendation_engine"' in content
    assert "has_table" in content or "has_column" in content
