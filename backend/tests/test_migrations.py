import ast
import os
import time
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config


def test_migration_revision_ids_fit_alembic_version_table():
    for path in Path("alembic/versions").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(target, ast.Name) and target.id == "revision" for target in node.targets):
                continue
            revision = ast.literal_eval(node.value)
            assert len(revision) <= 32, f"{path.name} revision is too long for alembic_version"


def test_report_migration_is_additive_and_guarded():
    migration = Path("alembic/versions/0002_reports_and_recommendation_context.py").read_text()

    assert "has_table(\"analysis_reports\")" in migration
    assert "drop_all" not in migration


def test_refinement_migration_is_additive_and_guarded():
    migration = Path("alembic/versions/0003_dkb_ai_budget_quant_refinements.py").read_text()

    assert "has_table(\"dkb_sync_logs\")" in migration
    assert "has_table(\"recommendation_attempts\")" in migration
    assert "drop_all" not in migration


def test_research_system_migration_is_additive_and_guarded():
    migration = Path("alembic/versions/0004_research_system.py").read_text()

    assert "has_table(\"assets\")" in migration
    assert "has_table(\"quant_experiments\")" in migration
    assert "recommendation_v2_payload_json" in migration
    assert "drop_all" not in migration


def test_wealth_cockpit_migration_is_additive_and_guarded():
    migration = Path("alembic/versions/0005_wealth_cockpit_foundations.py").read_text()

    assert "has_table(\"dkb_diagnostic_runs\")" in migration
    assert "has_table(\"review_items\")" in migration
    assert "has_table(\"provider_health\")" in migration
    assert "drop_all" not in migration


def test_restore_hypertable_writes_migration_is_additive_and_guarded():
    """ADR 0014: pin that the id-default + unique-index fix is present.

    2ce7981ac5c6_recreate_analytics silently dropped the effects of
    0073_hypertable_id_defaults and 0075_bar_prices_unique_index when it
    recreated these tables from scratch. This is a static pin (mirroring
    every other test in this file) that 0106 re-applies both fixes; the
    runtime invariant itself is checked against a live Postgres by
    test_hypertable_id_defaults_and_unique_indexes_exist below.
    """
    migration = Path("alembic/versions/0106_restore_hypertable_writes.py").read_text()

    assert '"bar_prices"' in migration
    assert '"factor_loadings_daily"' in migration
    assert '"regime_snapshots"' in migration
    assert '"provider_health_history"' in migration
    assert "{table}_id_seq" in migration
    assert "ALTER COLUMN id SET DEFAULT nextval" in migration
    assert "uq_bar_prices_symbol_ts" in migration
    assert "uq_factor_loadings_symbol_factor_ts" in migration
    assert "drop_table" not in migration


def _live_postgres_url() -> str | None:
    """Return a real Postgres DSN to test against, or None to skip.

    conftest.py forces DATABASE_URL to sqlite in-memory before app import (so
    the rest of the suite never touches a real database), which also hides
    the CI Postgres service from this process's env by the time tests run.
    Fall back to the well-known CI service address; skip cleanly wherever
    that isn't reachable (e.g. a plain local run without docker/postgres).
    The driver is named explicitly: psycopg 3 is the one installed, and a
    bare ``postgresql://`` resolves to psycopg2 under SQLAlchemy 2.0, which
    made this test skip in CI without anyone noticing.
    """
    return os.environ.get(
        "MIGRATION_TEST_DATABASE_URL",
        "postgresql+psycopg://postgres:postgres@localhost:5432/quantfolio",
    )


def test_hypertable_id_defaults_and_unique_indexes_exist():
    """ADR 0014 runtime invariant, against a real Postgres when one is reachable.

    After ``alembic upgrade head``, bar_prices/factor_loadings_daily/
    regime_snapshots/provider_health_history must all have an ``id`` column
    default (identity sequence), and bar_prices/factor_loadings_daily must
    carry their business-key UNIQUE index — the exact properties
    2ce7981ac5c6_recreate_analytics silently dropped.
    """
    url = _live_postgres_url()
    try:
        engine = sa.create_engine(url)
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
    except Exception:
        pytest.skip(f"no reachable Postgres at {url!r}; skipping live migration check")

    schema = f"migtest_{os.getpid()}"
    with engine.begin() as conn:
        # The extensions live in public, as on prod, so 0014/0020 find them and
        # the teardown below drops only tables. Created inside the throwaway
        # schema instead, the extension drop races TimescaleDB's background
        # workers (deadlock) and can strand the extension for the next run.
        conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS timescaledb SCHEMA public"))
        conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector SCHEMA public"))
        conn.execute(sa.text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.execute(sa.text(f"CREATE SCHEMA {schema}"))

    scoped_engine = sa.create_engine(url, connect_args={"options": f"-csearch_path={schema},public"})
    try:
        cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
        with scoped_engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")

        with scoped_engine.connect() as conn:
            inspector = sa.inspect(conn)
            for table in (
                "bar_prices",
                "factor_loadings_daily",
                "regime_snapshots",
                "provider_health_history",
            ):
                columns = {c["name"]: c for c in inspector.get_columns(table, schema=schema)}
                assert columns["id"].get("default"), f"{table}.id has no default"

            for table, index_name in (
                ("bar_prices", "uq_bar_prices_symbol_ts"),
                ("factor_loadings_daily", "uq_factor_loadings_symbol_factor_ts"),
            ):
                names = {ix["name"] for ix in inspector.get_indexes(table, schema=schema)}
                assert index_name in names, f"{index_name} missing on {table}"
    finally:
        scoped_engine.dispose()
        _drop_schema(engine, schema)


def _drop_schema(engine: sa.Engine, schema: str, attempts: int = 3) -> None:
    """Drop the throwaway schema, retrying if a TimescaleDB background job
    holds a lock on one of its hypertables at that moment."""
    for attempt in range(attempts):
        try:
            with engine.begin() as conn:
                conn.execute(sa.text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
            return
        except sa.exc.OperationalError:
            if attempt == attempts - 1:
                raise
            time.sleep(1)
