"""Archive, then reset, the decision-loop/paper-trading tables (ADR 0015,
Phase 2, rulings #2/#18: "archive, then reset, keep it running").

Scope is deliberately narrow: only tables holding paper-trading/discovery/
graduation/alphacrafter search history generated under the pre-Phase-1 broken
n_trials measurement. It never touches auth (users, api_keys, app_settings),
the real DKB-synced book (portfolios, holdings), price/factor caches, or the
Phase 1 global trial_ledger (that table IS the honesty mechanism going
forward — wiping it would be self-defeating).

Tables are checked against the live schema before use — a target database may
be behind HEAD (e.g. Phase 1's trial_ledger migration not yet deployed), and
this must degrade to "skip, note it, keep going" rather than crash on the
first name mismatch.

No schema change: this only TRUNCATEs rows, so no Alembic replay and no
service restart is required ("keep it running" satisfied literally).

Usage (run from backend/, against the real DATABASE_URL, Postgres only):
    .venv/bin/python scripts/archive_reset_decision_loop.py --dry-run
    .venv/bin/python scripts/archive_reset_decision_loop.py --archive-dir /var/backups/quantfolio --yes
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone

import sqlalchemy as sa

# Tables to archive-then-truncate. Every one is decision-loop/paper-trading/
# search-history state; nothing here is the real book, auth, or the Phase 1
# global ledger.
DECISION_LOOP_TABLES = [
    "paper_portfolios", "paper_holdings", "paper_trades", "paper_snapshots",
    "llm_portfolio_decisions", "llm_advice_cards",
    "advisor_scorecards", "advisor_strategies", "strategy_lessons",
    "advisor_graduation_state", "advisor_grad_transitions",
    "discover_runs", "discover_candidates", "discovery_prediction",
    "discovery_skill_snapshot", "discovery_config", "discovery_config_review",
    "factors_library", "alpha_signals", "screener_runs", "trader_backtests",
    "recommendation_dossiers", "alphacrafter_job_runs",
    "alphacrafter_tuning_trials", "ac_trial_ledger",
    "graduation_assessments",
    "competition_runs", "competition_decisions",
]

# Explicitly excluded, for reference: trial_ledger, users, api_keys,
# app_settings, portfolios, holdings, bar_prices, regime_snapshots, and every
# other market-data/audit/tax table.
CONTROL_TABLES = ["users", "api_keys", "app_settings", "portfolios", "holdings", "trial_ledger"]


def _postgres_url(database_url: str) -> str:
    if database_url.startswith("postgresql+"):
        return "postgresql:" + database_url.split(":", 1)[1]
    return database_url


def _pg_dump_target(database_url: str) -> tuple[str, dict[str, str]]:
    """A password-free libpq URL for argv, plus the environment that carries the password.

    argv is world-readable through /proc/<pid>/cmdline and ``ps``; the environment
    of a process is not. libpq reads ``PGPASSWORD``, so the password goes there.
    """
    url = sa.engine.make_url(_postgres_url(database_url))
    env = dict(os.environ)
    if url.password:
        env["PGPASSWORD"] = url.password
    bare = sa.engine.URL.create(
        url.drivername, username=url.username, host=url.host, port=url.port,
        database=url.database, query=url.query,
    )
    return bare.render_as_string(hide_password=False), env


def _existing_tables(conn: sa.Connection, tables: list[str]) -> set[str]:
    """Which of ``tables`` actually exist in the connected database's public
    schema right now. A target may be behind HEAD (a migration not yet
    deployed) or a name may simply be wrong — either way we must not crash,
    just report it and move on."""
    rows = conn.execute(
        sa.text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = ANY(:names)"
        ),
        {"names": tables},
    )
    return {r[0] for r in rows}


def _row_counts(conn: sa.Connection, tables: list[str], present: set[str]) -> dict[str, int | None]:
    """Row counts for ``tables``; a table not in ``present`` gets ``None``
    (not 0 — 0 would falsely claim "empty and safe", None means "not here")."""
    counts: dict[str, int | None] = {}
    for table in tables:
        if table not in present:
            counts[table] = None
            continue
        # table is always drawn from this module's own hardcoded constant
        # lists (DECISION_LOOP_TABLES/CONTROL_TABLES), never external input,
        # and is additionally checked against information_schema above.
        result = conn.execute(sa.text(f'SELECT count(*) FROM "{table}"'))  # noqa: S608
        counts[table] = result.scalar_one()
    return counts


def _print_counts(label: str, counts: dict[str, int | None]) -> None:
    print(label)
    for table, count in counts.items():
        shown = "not present (skipped)" if count is None else str(count)
        print(f"  {table:32s} {shown}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", default=None, help="Defaults to $DATABASE_URL")
    parser.add_argument("--archive-dir", default="/var/backups/quantfolio")
    parser.add_argument("--dry-run", action="store_true", help="Print row counts only, no dump, no truncate")
    parser.add_argument("--yes", action="store_true", help="Required to actually truncate (skipped for --dry-run)")
    args = parser.parse_args()

    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL must be set (env or --database-url).", file=sys.stderr)
        return 1

    engine = sa.create_engine(database_url)
    with engine.connect() as conn:
        present_decision = _existing_tables(conn, DECISION_LOOP_TABLES)
        present_control = _existing_tables(conn, CONTROL_TABLES)
        before = _row_counts(conn, DECISION_LOOP_TABLES, present_decision)
        control_before = _row_counts(conn, CONTROL_TABLES, present_control)

    _print_counts("Decision-loop table row counts (to be archived + truncated):", before)
    _print_counts("\nControl tables (must be unchanged after; 'not present' just means not migrated here yet):", control_before)

    missing_decision = [t for t in DECISION_LOOP_TABLES if t not in present_decision]
    if missing_decision:
        print(f"\nNote: {len(missing_decision)} decision-loop table(s) don't exist on this "
              f"database yet and will be skipped: {', '.join(missing_decision)}")

    if args.dry_run:
        print("\n--dry-run: no dump, no truncate performed.")
        return 0

    if not args.yes:
        print("\nRefusing to truncate without --yes (run --dry-run first to review counts).", file=sys.stderr)
        return 1

    active_tables = [t for t in DECISION_LOOP_TABLES if t in present_decision]
    if not active_tables:
        print("\nNo decision-loop tables exist on this database — nothing to archive or truncate.", file=sys.stderr)
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dump_path = f"{args.archive_dir}/decision_loop_archive_{stamp}.dump"
    pg_url, dump_env = _pg_dump_target(database_url)

    dump_cmd = ["pg_dump", pg_url, "--format=custom", "--data-only", f"--file={dump_path}"]
    for table in active_tables:
        dump_cmd += ["-t", table]
    print(f"\nDumping data-only archive to {dump_path} ...")
    subprocess.run(dump_cmd, check=True, env=dump_env)

    truncate_list = ", ".join(f'"{t}"' for t in active_tables)
    with engine.begin() as conn:
        print("Truncating decision-loop tables in one transaction ...")
        conn.execute(sa.text(f"TRUNCATE TABLE {truncate_list} RESTART IDENTITY CASCADE"))

    with engine.connect() as conn:
        after = _row_counts(conn, DECISION_LOOP_TABLES, present_decision)
        control_after = _row_counts(conn, CONTROL_TABLES, present_control)

    assert all(v == 0 for t, v in after.items() if t in present_decision), f"truncate incomplete: {after}"
    assert control_before == control_after, (
        f"control tables changed! before={control_before} after={control_after}"
    )

    print(f"\nDone. Archive: {dump_path}")
    print("All decision-loop tables at 0 rows; control tables unchanged.")
    print("Restore later via: pg_restore --data-only -d <db> " + dump_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
