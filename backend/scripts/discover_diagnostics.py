"""Read-only diagnostics for the Discover pipeline.

Explains *why* discovery rejected candidates by replaying the real pipeline
stage functions against the live database and dumping the underlying stored
price data (bar count, date range, providers, currencies, price levels).

Nothing is written. Safe to run against production.

Usage (on the deployment host, where the DB is reachable):

    cd backend && .venv/bin/python scripts/discover_diagnostics.py
    # optional: restrict to specific symbols
    cd backend && .venv/bin/python scripts/discover_diagnostics.py SAP.DE AIR.DE BMW.DE
    # optional: pick a run id (defaults to the most recent run)
    cd backend && .venv/bin/python scripts/discover_diagnostics.py --run <run_id>
"""
from __future__ import annotations

import pathlib
import sys
from urllib.parse import urlsplit, urlunsplit

# Make `import app` work regardless of CWD: backend/ is the parent of scripts/.
_BACKEND_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import pandas as pd  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.foundation.core.config import get_settings  # noqa: E402
from app.foundation.core.db import SessionLocal  # noqa: E402
from app.decision.discover.pipeline import (  # noqa: E402
    _pick_benchmark,
    stage_backtest_vs_benchmark,
    stage_history_ingest,
    stage_momentum_quality,
    stage_portfolio_fit,
    stage_verification_gate,
)
from app.foundation.market import history as market_history  # noqa: E402
from app.foundation.portfolio.bridge import build_real_price_matrix  # noqa: E402


def _hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def _raw_bar_summary(db, symbol: str) -> str:
    """Per-symbol stored-data fingerprint straight from bar_prices."""
    try:
        row = db.execute(
            text(
                """
                SELECT count(*) AS n,
                       min(ts) AS first_ts,
                       max(ts) AS last_ts,
                       count(DISTINCT provider) AS n_providers,
                       string_agg(DISTINCT provider, ',') AS providers,
                       string_agg(DISTINCT currency, ',') AS currencies,
                       min(close) AS min_close,
                       max(close) AS max_close
                FROM bar_prices WHERE symbol = :s
                """
            ),
            {"s": symbol.upper()},
        ).mappings().first()
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - sqlite lacks string_agg
        return f"(bar_prices query failed: {exc})"
    if not row or not row["n"]:
        return "NO ROWS in bar_prices"
    return (
        f"bars={row['n']} range={row['first_ts']}..{row['last_ts']} "
        f"providers=[{row['providers']}] currencies=[{row['currencies']}] "
        f"close_min={row['min_close']} close_max={row['max_close']}"
    )


def diagnose_symbol(db, symbol: str, is_etf: bool) -> None:
    _hr(f"SYMBOL {symbol}  (is_etf={is_etf})")
    print("stored:", _raw_bar_summary(db, symbol))

    rows = market_history(db, symbol, days=365 * 5, allow_live=False)
    print(f"market_history(5y, cache-only): {len(rows)} rows")
    if rows:
        df = pd.DataFrame(rows).dropna(subset=["close"]).sort_values("date")
        if len(df) >= 2:
            first = df.iloc[0]
            last = df.iloc[-1]
            print(
                f"  first={first['date']} close={first['close']}  "
                f"last={last['date']} close={last['close']}  "
                f"date dtype={type(df['date'].iloc[0]).__name__}"
            )
            # Flag suspicious gaps (potential source-mixing / split / currency jumps)
            pc = df["close"].pct_change().abs()
            jumps = pd.DataFrame(df.loc[pc > 0.30, ["date", "close"]])
            if not jumps.empty:
                print(f"  WARN {jumps.shape[0]} day-over-day jumps >30% (possible source/split/currency mix):")
                for _, j in jumps.head(8).iterrows():
                    print(f"      {j['date']}  close={j['close']}")

    # Replay the real stage functions in order, stopping at first reject.
    print("  -- stage replay --")
    s, r = stage_history_ingest(db, symbol)
    print(f"  [1] history_ingest : {'REJECT ' + r if r else 'pass ' + str(s)}")
    if r:
        return
    s, r = stage_momentum_quality(db, symbol)
    print(f"  [2] momentum       : {'REJECT ' + r if r else 'pass ' + str(s)}")
    if r:
        return
    bench = _pick_benchmark(symbol, is_etf)
    print(f"      benchmark used = {bench}  ({_raw_bar_summary(db, bench)})")
    s, r = stage_backtest_vs_benchmark(db, symbol, is_etf=is_etf)
    print(f"  [3] backtest       : {'REJECT ' + r if r else 'pass ' + str(s)}")
    if r:
        return
    s, r = stage_verification_gate(db, symbol)
    print(f"  [4] verification   : {'REJECT ' + r if r else 'pass ' + str(s)}")
    if r:
        return


def diagnose_portfolio(db, user_id: str) -> None:
    _hr(f"PORTFOLIO MATRIX for user {user_id}")
    port_df = build_real_price_matrix(db, user_id, lookback_days=365)
    if port_df is None or port_df.empty:
        print("build_real_price_matrix returned None/empty -> portfolio_fit yields NEUTRAL (no reject)")
        return
    print(f"matrix shape = {port_df.shape}  (rows x tickers)")
    print(f"index dtype  = {port_df.index.dtype}")
    print(f"date range   = {port_df.index.min()} .. {port_df.index.max()}")
    print(f"tickers      = {list(port_df.columns)}")
    port_rets = port_df.pct_change().dropna()
    port_weighted = pd.Series(port_rets.mean(axis=1))
    print(f"portfolio return observations = {port_weighted.shape[0]}")
    print(
        "\nNOTE: stage_portfolio_fit requires >=30 dates shared between the "
        "candidate and this portfolio series, else it REJECTS with "
        "'insufficient aligned observations'."
    )


def _redact_db_url(url: str) -> str:
    """Return a credential-free representation of a DB URL for safe logging.

    Strips userinfo (``user:pass@``) and the query string (which can carry a
    ``password=`` parameter), keeping scheme/host/port/path. URLs with nothing
    that can hold a credential (e.g. ``sqlite:///path``) are returned verbatim
    so the path stays exact.
    """
    try:
        parsed = urlsplit(url)
        if not (parsed.username or parsed.password or parsed.query):
            return url
        host = parsed.hostname or ""
        netloc = host + (f":{parsed.port}" if parsed.port else "")
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except Exception:  # noqa: BLE001 - logging helper must never raise
        return "(unparseable database url)"


def main() -> None:
    args = sys.argv[1:]
    run_id = None
    if "--run" in args:
        i = args.index("--run")
        if i + 1 >= len(args):
            print("error: --run requires a run id argument")
            sys.exit(2)
        run_id = args[i + 1]
        args = args[:i] + args[i + 2 :]
    explicit_symbols = [a for a in args if not a.startswith("--")]

    settings = get_settings()
    print(f"DATABASE_URL = {_redact_db_url(settings.database_url)}")

    db = SessionLocal()
    try:
        # Resolve the run + its user/candidates
        if run_id:
            run = db.execute(
                text("SELECT id, user_id FROM discover_runs WHERE id = :r"), {"r": run_id}
            ).mappings().first()
        else:
            run = db.execute(
                text("SELECT id, user_id FROM discover_runs ORDER BY created_at DESC LIMIT 1")
            ).mappings().first()
        if not run:
            print("No DiscoverRun found. Pass symbols explicitly to diagnose them.")
            user_id = None
            cands = []
        else:
            run_id = run["id"]
            user_id = run["user_id"]
            print(f"Using run {run_id}  user {user_id}")
            cands = db.execute(
                text(
                    "SELECT symbol, source, reject_stage, reject_reason "
                    "FROM discover_candidates WHERE run_id = :r ORDER BY reject_stage, symbol"
                ),
                {"r": run_id},
            ).mappings().all()

        if explicit_symbols:
            symbols = [(s, False) for s in explicit_symbols]
        else:
            symbols = [(c["symbol"], c["source"] == "screen_etf") for c in cands]

        # Reject-reason histogram
        if cands:
            _hr("REJECT-STAGE HISTOGRAM (from DB)")
            hist: dict[str, int] = {}
            for c in cands:
                hist[c["reject_stage"] or "passed"] = hist.get(c["reject_stage"] or "passed", 0) + 1
            for stage, n in sorted(hist.items(), key=lambda x: -x[1]):
                print(f"  {stage:28s} {n}")

        if user_id:
            diagnose_portfolio(db, user_id)

        _hr(f"PER-SYMBOL DIAGNOSIS ({len(symbols)} symbols)")
        for sym, is_etf in symbols:
            try:
                diagnose_symbol(db, sym, is_etf)
                if user_id:
                    s, r = stage_portfolio_fit(db, user_id, sym, is_etf=is_etf)
                    print(f"  [5] portfolio_fit  : {'REJECT ' + r if r else 'pass ' + str(s)}")
            except Exception as exc:  # noqa: BLE001
                print(f"  !! diagnosis crashed for {sym}: {exc}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
