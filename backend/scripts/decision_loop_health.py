"""Read-only health check of the live decision loop, against production data.

Answers the questions the code alone cannot: did the advisor's optimiser ever
run (or fall back to equal weight every cycle), did the paper sleeves beat the
passive core, do Discover's convictions rank realised returns at all, and is
the prediction ledger's "hit rate" anything more than market beta.

Nothing is written. Every query runs in its own READ ONLY transaction, so a
failing query is reported and skipped without affecting the rest. PostgreSQL
only (the production dialect); the regex/date_trunc SQL is not SQLite-safe.

Usage, on the app LXC (it already holds the repo and the DATABASE_URL):

    cd /opt/quantfolio/backend
    set -a && . ../.env && set +a
    .venv/bin/python scripts/decision_loop_health.py --out /tmp/decision_loop_health.md

Options:
    --bench EUNL.DE   benchmark ticker in price_cache (default: the passive core)
    --days 180        look-back window for the time-bucketed sections
    --out PATH        also write the report as Markdown to PATH
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from typing import Any

# Make `import app` work regardless of CWD: backend/ is the parent of scripts/.
_BACKEND_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import pandas as pd  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.foundation.core.db import engine  # noqa: E402

QUERIES: list[tuple[str, str, str]] = [
    (
        "1. Advisor cycles: status and optimiser outcome per week",
        "An optimiser_status of 'failed' or a note containing 'equal-weight fallback' "
        "means the skfolio step did not produce weights and the LLM was shown 1/N targets. "
        "Before the risk-free-rate unit fix, MAXIMIZE_RATIO failed whenever rf was annual.",
        """
        SELECT date_trunc('week', review_date)::date AS week,
               mandate,
               status,
               COALESCE(substring(decision_json FROM '"optimizer_status": "([a-z_]+)"'), '(none)')
                   AS optimizer_status,
               count(*) AS cycles,
               sum(CASE WHEN decision_json LIKE '%equal-weight fallback%' THEN 1 ELSE 0 END)
                   AS equal_weight_fallbacks
        FROM llm_portfolio_decisions
        WHERE review_date >= now() - make_interval(days => :days)
        GROUP BY 1, 2, 3, 4
        ORDER BY 1 DESC, 2, 3, 4
        """,
    ),
    (
        "2. Paper portfolios vs the passive core (same window)",
        "Simple first-to-last snapshot return. Paper books have no external cash flows "
        "after seeding, so this is comparable to the benchmark's price return.",
        """
        WITH span AS (
            SELECT portfolio_id, min(date) AS first_date, max(date) AS last_date, count(*) AS n_snapshots
            FROM paper_snapshots
            GROUP BY portfolio_id
        ),
        ends AS (
            SELECT s.*,
                   (SELECT total_value FROM paper_snapshots p
                     WHERE p.portfolio_id = s.portfolio_id AND p.date = s.first_date LIMIT 1) AS v0,
                   (SELECT total_value FROM paper_snapshots p
                     WHERE p.portfolio_id = s.portfolio_id AND p.date = s.last_date LIMIT 1) AS v1,
                   (SELECT close FROM price_cache
                     WHERE ticker = :bench AND date <= s.first_date ORDER BY date DESC LIMIT 1) AS b0,
                   (SELECT close FROM price_cache
                     WHERE ticker = :bench AND date <= s.last_date ORDER BY date DESC LIMIT 1) AS b1
            FROM span s
        )
        SELECT pp.name, pp.mandate, pp.managed_by, e.first_date, e.last_date, e.n_snapshots,
               round(((e.v1 / NULLIF(e.v0, 0)) - 1) * 100, 2) AS paper_return_pct,
               round(((e.b1 / NULLIF(e.b0, 0)) - 1) * 100, 2) AS bench_return_pct,
               round((((e.v1 / NULLIF(e.v0, 0)) - (e.b1 / NULLIF(e.b0, 0)))) * 100, 2) AS excess_pct_points
        FROM ends e
        JOIN paper_portfolios pp ON pp.id = e.portfolio_id
        ORDER BY e.last_date DESC
        """,
    ),
    (
        "3. Paper trading activity: turnover and fees",
        "turnover_x_nav = gross traded value / average NAV. Under German taxation every "
        "realised gain is taxed, so annualised turnover well above ~0.3x is a drag the "
        "paper book does not even charge itself for.",
        """
        WITH nav AS (
            SELECT portfolio_id, avg(total_value) AS avg_nav,
                   greatest(max(date) - min(date), 1) AS days
            FROM paper_snapshots GROUP BY portfolio_id
        )
        SELECT pp.name, pp.mandate,
               count(t.id) AS trades,
               round(sum(t.value), 0) AS gross_traded_eur,
               round(sum(COALESCE(t.fee, 0)), 2) AS fees_eur,
               round(sum(t.value) / NULLIF(n.avg_nav, 0), 2) AS turnover_x_nav,
               round(sum(t.value) / NULLIF(n.avg_nav, 0) * 365.0 / n.days, 2) AS annualised_turnover_x_nav,
               min(t.date) AS first_trade, max(t.date) AS last_trade
        FROM paper_portfolios pp
        JOIN paper_trades t ON t.portfolio_id = pp.id
        LEFT JOIN nav n ON n.portfolio_id = pp.id
        GROUP BY pp.name, pp.mandate, n.avg_nav, n.days
        ORDER BY trades DESC
        """,
    ),
    (
        "4. Paper holdings: pricing freshness and quote currency",
        "The paper book is labelled EUR but marks each holding at its native quote. "
        "Any non-EUR currency here means FX is ignored in paper P&L.",
        """
        SELECT pp.name AS portfolio, h.ticker, h.quantity,
               (SELECT max(date) FROM price_cache pc WHERE pc.ticker = h.ticker) AS last_price_date,
               (SELECT currency FROM price_cache pc WHERE pc.ticker = h.ticker
                 ORDER BY date DESC LIMIT 1) AS quote_currency
        FROM paper_holdings h
        JOIN paper_portfolios pp ON pp.id = h.portfolio_id
        WHERE h.quantity > 0
        ORDER BY last_price_date NULLS FIRST, pp.name, h.ticker
        """,
    ),
    (
        "5. Discover prediction ledger: outcomes",
        "Two sources share the ledger: Discover's weekly shortlist (portfolio_id NULL) and the "
        "advisor's daily calls (one row per ticker and paper sleeve). hit_vs_bench is the "
        "scorer's hit (excess_return > 0); hit_rate_raw only says the price went up. A high "
        "'delisted' share usually means no fresh cached price, not a real delisting.",
        """
        SELECT CASE WHEN portfolio_id IS NULL THEN 'discover' ELSE 'advisor' END AS source,
               outcome_status, direction, count(*) AS n,
               round(avg(realised_return)::numeric * 100, 2) AS avg_realised_pct,
               round(avg(excess_return)::numeric * 100, 2) AS avg_excess_pct,
               round(avg(CASE WHEN realised_return > 0 THEN 1.0 ELSE 0.0 END)::numeric, 3) AS hit_rate_raw,
               round(avg(CASE WHEN excess_return > 0 THEN 1.0 WHEN excess_return IS NULL THEN NULL
                              ELSE 0.0 END)::numeric, 3) AS hit_vs_bench,
               round(avg(conviction)::numeric, 3) AS avg_conviction
        FROM discovery_prediction
        GROUP BY 1, 2, 3
        ORDER BY 1, 2, 3
        """,
    ),
    (
        "5b. When the record becomes judgeable",
        "Each prediction is judged over 21 trading days (~29 calendar days), so weekly dates "
        "share most of their window: count months, not weeks. The app shows a hit rate and "
        "an IC t-statistic only once Discover's resolved record spans 12 non-overlapping "
        "months (Discover -> Prediction Skill).",
        """
        SELECT CASE WHEN portfolio_id IS NULL THEN 'discover' ELSE 'advisor' END AS source,
               date_trunc('month', predicted_at)::date AS predicted_month,
               count(*) AS n,
               count(DISTINCT predicted_at::date) AS dates,
               count(DISTINCT symbol) AS symbols,
               sum(CASE WHEN outcome_status = 'resolved' THEN 1 ELSE 0 END) AS resolved,
               sum(CASE WHEN outcome_status = 'pending' THEN 1 ELSE 0 END) AS pending,
               min(resolve_at)::date AS first_resolve,
               max(resolve_at)::date AS last_resolve
        FROM discovery_prediction
        GROUP BY 1, 2
        ORDER BY 1, 2
        """,
    ),
    (
        "6. Discover predictions judged against the benchmark (Discover rows only)",
        "hit_vs_bench is the hit rate a benchmark-relative scorer would report. The gap "
        "between hit_raw and hit_vs_bench is market beta, not skill. avg_resolution_gap_pct "
        "is how far the resolver's price (latest cached close at resolution time) sits from "
        "the close on the horizon date itself.",
        """
        WITH r AS (
            SELECT p.direction, p.conviction, p.realised_return, p.price_at_prediction,
                   (SELECT close FROM price_cache
                     WHERE ticker = :bench AND date <= p.predicted_at::date ORDER BY date DESC LIMIT 1) AS b0,
                   (SELECT close FROM price_cache
                     WHERE ticker = :bench AND date <= p.resolve_at::date ORDER BY date DESC LIMIT 1) AS b1,
                   (SELECT close FROM price_cache
                     WHERE ticker = p.symbol AND date <= p.resolve_at::date ORDER BY date DESC LIMIT 1)
                       AS px_at_horizon
            FROM discovery_prediction p
            WHERE p.outcome_status = 'resolved' AND p.realised_return IS NOT NULL
              AND p.portfolio_id IS NULL
        ),
        s AS (
            -- A "sell" call is judged against the benchmark's move with the
            -- sign flipped, exactly as discover/resolution.py flips the
            -- candidate's own return.
            SELECT r.*,
                   CASE WHEN direction IN ('sell', 'bear') THEN -1 ELSE 1 END AS sign,
                   CASE WHEN direction IN ('sell', 'bear') THEN -(b1 / b0 - 1) ELSE (b1 / b0 - 1) END
                       AS signed_bench
            FROM r
            WHERE b0 IS NOT NULL AND b1 IS NOT NULL AND b0 > 0
        )
        SELECT count(*) AS n,
               round(avg(realised_return)::numeric * 100, 2) AS avg_realised_pct,
               round(avg(signed_bench)::numeric * 100, 2) AS avg_signed_bench_pct,
               round(avg(realised_return - signed_bench)::numeric * 100, 2) AS avg_excess_pct,
               round(avg(CASE WHEN realised_return > 0 THEN 1.0 ELSE 0.0 END)::numeric, 3) AS hit_raw,
               round(avg(CASE WHEN realised_return > signed_bench THEN 1.0 ELSE 0.0 END)::numeric, 3)
                   AS hit_vs_bench,
               round(avg(abs(
                   (px_at_horizon / NULLIF(price_at_prediction, 0) - 1) * sign
                   - realised_return))::numeric * 100, 2) AS avg_resolution_gap_pct
        FROM s
        """,
    ),
    (
        "7. Does conviction rank outcomes? Weekly Spearman IC, Discover only (conviction vs excess return)",
        "Rank correlation within each prediction week (>= 5 resolved rows). Advisor rows are left "
        "out: their conviction is an LLM confidence on another scale. t_naive treats weeks as "
        "independent; they overlap about four deep, so it overstates |t| (roughly 2x). The "
        "app's Discover -> Prediction Skill shows the Newey-West t.",
        """
        WITH r AS (
            SELECT date_trunc('week', predicted_at)::date AS wk,
                   rank() OVER (PARTITION BY date_trunc('week', predicted_at) ORDER BY conviction) AS rc,
                   rank() OVER (PARTITION BY date_trunc('week', predicted_at)
                                ORDER BY coalesce(excess_return, realised_return)) AS rr
            FROM discovery_prediction
            WHERE outcome_status = 'resolved' AND realised_return IS NOT NULL AND conviction IS NOT NULL
              AND portfolio_id IS NULL
        ),
        weekly AS (
            SELECT wk, count(*) AS n, corr(rc, rr) AS ic FROM r GROUP BY wk HAVING count(*) >= 5
        )
        SELECT count(*) AS weeks, sum(n) AS predictions,
               round(avg(ic)::numeric, 3) AS mean_ic,
               round(stddev(ic)::numeric, 3) AS sd_ic,
               round((avg(ic) / NULLIF(stddev(ic), 0) * sqrt(count(*)))::numeric, 2) AS t_naive
        FROM weekly
        """,
    ),
    (
        "8. Regime snapshots per week",
        "Which producer (source) is writing, and whether the label ever changes.",
        """
        SELECT date_trunc('week', ts)::date AS week, source, label, count(*) AS n,
               round(avg(score)::numeric, 3) AS avg_score
        FROM regime_snapshots
        WHERE ts >= now() - make_interval(days => :days)
        GROUP BY 1, 2, 3
        ORDER BY 1 DESC, 2, 3
        """,
    ),
    (
        "9. Per-ticker ML models",
        "'completed' models are the only ones stage_ml_signal reads.",
        """
        SELECT status, feature_schema_version, count(*) AS n, count(DISTINCT ticker) AS tickers,
               max(finished_at) AS latest
        FROM quant_ml_models
        GROUP BY 1, 2
        ORDER BY 1, 2
        """,
    ),
    (
        "10. Scheduled jobs, last 14 days",
        "Silent-failure check: a job that never appears here is not running at all.",
        """
        SELECT job_name, status, count(*) AS runs, max(started_at) AS latest,
               round(avg(duration_ms) / 1000.0, 1) AS avg_seconds
        FROM job_runs
        WHERE started_at >= now() - interval '14 days'
        GROUP BY 1, 2
        ORDER BY 1, 2
        """,
    ),
    (
        "11. Recommendations surfaced (last 90 days)",
        "Everything with verdict BUY here reached the UI without passing the graduation gate "
        "unless mode is the advisor path.",
        """
        SELECT mode, verdict, approval_state, count(*) AS n, max(created_at) AS latest
        FROM recommendations
        WHERE created_at >= now() - interval '90 days'
        GROUP BY 1, 2, 3
        ORDER BY n DESC
        """,
    ),
    (
        "12. Discover runs per week",
        "",
        """
        SELECT date_trunc('week', created_at)::date AS week, status, count(*) AS runs
        FROM discover_runs
        WHERE created_at >= now() - make_interval(days => :days)
        GROUP BY 1, 2
        ORDER BY 1 DESC, 2
        """,
    ),
    (
        "13. Trial ledger and strategies",
        "",
        """
        SELECT 'trial_ledger:' || context AS item, count(*) AS n FROM trial_ledger GROUP BY context
        UNION ALL
        SELECT 'advisor_strategies:' || role, count(*) FROM advisor_strategies GROUP BY role
        ORDER BY 1
        """,
    ),
    (
        "14. Settings that change decision-loop behaviour",
        "risk_free_rate_pct is an annual fraction. decision_loop_mode is currently only "
        "echoed by the graduation API; no job reads it.",
        """
        SELECT key, value_json
        FROM app_settings
        WHERE key IN ('risk_free_rate_pct', 'risk_free_rate_as_of', 'decision_loop_mode',
                      'passive_core_ticker', 'scheduled_recommendation_refresh',
                      'scheduled_quant_experiment_refresh', 'er_mode', 'regime_index_symbol',
                      'alphacrafter_index_basket', 'tax_residency_country')
        ORDER BY key
        """,
    ),
]


def _run_one(sql: str, params: dict[str, Any]) -> pd.DataFrame:
    with engine.connect() as conn:
        # One READ ONLY transaction per query: a failure in one cannot poison
        # the next, and nothing can be written even by mistake.
        with conn.begin():
            conn.execute(text("SET TRANSACTION READ ONLY"))
            result = conn.execute(text(sql), params)
            return pd.DataFrame(result.fetchall(), columns=list(result.keys()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bench", default="EUNL.DE")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    if engine.dialect.name != "postgresql":
        print(f"This script targets PostgreSQL; DATABASE_URL points at {engine.dialect.name}.", file=sys.stderr)
        return 2

    params = {"bench": args.bench, "days": args.days}
    sections: list[str] = [
        "# Decision-loop health",
        f"benchmark `{args.bench}`, window {args.days} days",
    ]
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 30)
    pd.set_option("display.max_rows", 400)

    for title, explanation, sql in QUERIES:
        print(f"\n== {title}")
        if explanation:
            print(f"   {explanation}")
        sections.append(f"\n## {title}\n")
        if explanation:
            sections.append(f"{explanation}\n")
        try:
            frame = _run_one(sql, params)
        except Exception as exc:  # noqa: BLE001 — report and continue to the next query
            message = str(exc).splitlines()[0][:300]
            print(f"   QUERY FAILED: {message}")
            sections.append(f"QUERY FAILED: `{message}`\n")
            continue
        if frame.empty:
            print("   (no rows)")
            sections.append("(no rows)\n")
            continue
        print(frame.to_string(index=False))
        sections.append("```\n" + frame.to_string(index=False) + "\n```\n")

    if args.out:
        pathlib.Path(args.out).write_text("\n".join(sections), encoding="utf-8")
        print(f"\nWrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
