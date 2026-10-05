"""Tests for AlphaCrafter Trader: pure helpers and end-to-end run_trader."""

from __future__ import annotations

import asyncio
import json
import math
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import FactorsLibrary, ScreenerRun, TraderBacktest
from app.lab.alphacrafter.shared_memory import (
    SharedMemoryH,
)
from app.lab.alphacrafter.trader import (
    DEFAULT_COMMISSIONS,
    DEFAULT_MAX_POSITIONS,
    DEFAULT_POSITION_SIZES,
    DEFAULT_REBALANCE_FREQS,
    TraderAgent,
    TraderConfig,
    TraderResult,
    _generate_config_grid,
    _rebalance_dates,
    _trader_grid_settings,
    _zscore_cross_sectional,
    build_weights,
    composite_score,
    run_trader,
    strategy_equity,
)


# ---------------------------------------------------------------------------
# Test DB helpers
# ---------------------------------------------------------------------------


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS bar_prices (
                    symbol TEXT, ts TIMESTAMP, open REAL, high REAL, low REAL,
                    close REAL, volume REAL, currency TEXT, provider TEXT
                )
                """
            )
        )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


_WALK_FORWARD_ANCHOR = datetime(2026, 9, 15, 12, tzinfo=UTC)


def _seed_trend_bars(db, n_symbols: int = 5, days: int = 90, anchor: datetime | None = None):
    """Seed bars with DISTINCT linear trends per symbol so cross-sectional
    ranking is non-trivial.  Bars end two days before *anchor* (default: now)
    so build_panel's date-window query returns them."""
    symbols = [f"SYM{i}" for i in range(n_symbols)]
    base = (anchor or datetime.now(UTC)) - timedelta(days=days + 2)
    rng = np.random.default_rng(42)
    for i, sym in enumerate(symbols):
        # drift spans 0.001 to 0.005 — clearly different across symbols.
        drift = 0.001 * (i + 1)
        price = 100.0 * (1 + i * 0.1)  # different starting price too
        for d in range(days):
            price *= 1 + drift + rng.normal(0, 0.0002)
            ts = base + timedelta(days=d)
            db.execute(
                text(
                    """
                    INSERT INTO bar_prices
                      (symbol, ts, open, high, low, close, volume, currency, provider)
                    VALUES (:s, :ts, :p, :p, :p, :p, :v, 'USD', 'test')
                    """
                ),
                {"s": sym, "ts": ts, "p": float(price), "v": float(rng.uniform(1e6, 9e6))},
            )
    db.commit()
    return symbols


def _seed_factors_and_screener(db, symbols: list[str]):
    """Persist two price-only FactorsLibrary rows and a ScreenerRun that selects
    them.  Uses DSL factors (rank(close), rank(volume)) which are whitelisted bare
    variables — no fundamentals needed."""
    f1 = FactorsLibrary(
        name="test_rank_close",
        formula_json=json.dumps({"formula": "rank(close)", "dsl": "rank(close)", "name": "test_rank_close"}),
        source="test",
        ic_summary_json=json.dumps({"ic": 0.05, "icir": 1.0, "ic_series": [0.05] * 10}),
    )
    f2 = FactorsLibrary(
        name="test_neg_rank_vol",
        formula_json=json.dumps({"formula": "rank(volume)", "dsl": "rank(volume)", "name": "test_neg_rank_vol"}),
        source="test",
        ic_summary_json=json.dumps({"ic": -0.03, "icir": -0.8, "ic_series": [-0.03] * 10}),
    )
    db.add(f1)
    db.add(f2)
    db.flush()

    screener_run = ScreenerRun(
        regime_label="bull",
        selected_factor_ids=f"{f1.id},{f2.id}",
        scores_json=json.dumps({}),
    )
    db.add(screener_run)
    db.commit()
    return f1, f2, screener_run


# ---------------------------------------------------------------------------
# 1. _zscore_cross_sectional
# ---------------------------------------------------------------------------


class TestZscoreCrossSectional:
    def test_row_mean_is_near_zero(self):
        idx = pd.date_range("2024-01-01", periods=5, freq="D")
        data = {"A": [1.0, 2.0, 3.0, 4.0, 5.0], "B": [3.0, 4.0, 5.0, 6.0, 7.0], "C": [5.0, 6.0, 7.0, 8.0, 9.0]}
        df = pd.DataFrame(data, index=idx)
        z = _zscore_cross_sectional(df)
        row_means = z.mean(axis=1)
        assert (row_means.abs() < 1e-10).all(), f"Row means not near zero: {row_means.values}"

    def test_row_std_is_near_one_on_non_degenerate_row(self):
        rng = np.random.default_rng(7)
        idx = pd.date_range("2024-01-01", periods=10, freq="D")
        df = pd.DataFrame(rng.normal(size=(10, 6)), index=idx, columns=list("ABCDEF"))
        z = _zscore_cross_sectional(df)
        row_stds = z.std(axis=1, ddof=1)
        # Should be close to 1 for every non-degenerate row.
        assert (row_stds.dropna() - 1.0).abs().max() < 0.1, f"Row stds off: {row_stds.values}"

    def test_zero_std_row_yields_nan(self):
        """A row where all symbols have the same value → std=0 → z-scores are NaN."""
        idx = pd.date_range("2024-01-01", periods=3, freq="D")
        df = pd.DataFrame({"A": [1.0, 2.0, 3.0], "B": [1.0, 3.0, 3.0], "C": [1.0, 4.0, 3.0]})
        df.index = idx
        # Row 0 is constant: A=B=C=1
        z = _zscore_cross_sectional(df)
        assert z.iloc[0].isna().all(), "Constant row should produce all-NaN z-scores"

    def test_output_shape_matches_input(self):
        idx = pd.date_range("2024-01-01", periods=8, freq="D")
        df = pd.DataFrame(np.arange(24, dtype=float).reshape(8, 3), index=idx, columns=list("XYZ"))
        z = _zscore_cross_sectional(df)
        assert z.shape == df.shape


# ---------------------------------------------------------------------------
# 2. composite_score
# ---------------------------------------------------------------------------


class TestCompositeScore:
    def _make_panel(self, n_dates: int = 20, n_symbols: int = 4, seed: int = 0) -> dict:
        rng = np.random.default_rng(seed)
        idx = pd.date_range("2024-01-01", periods=n_dates, freq="D")
        cols = [f"S{i}" for i in range(n_symbols)]
        close = pd.DataFrame(100 + rng.standard_normal((n_dates, n_symbols)).cumsum(axis=0), index=idx, columns=cols)
        volume = pd.DataFrame(rng.uniform(1e6, 9e6, size=(n_dates, n_symbols)), index=idx, columns=cols)
        return {"close": close, "volume": volume}

    def test_returns_dataframe_with_panel_shape(self):
        panel = self._make_panel()
        f = FactorsLibrary(
            name="test_cs",
            formula_json=json.dumps({"formula": "rank(close)", "dsl": "rank(close)", "name": "test_cs"}),
            source="test",
            ic_summary_json=json.dumps({"ic": 0.1, "icir": 1.0, "ic_series": []}),
        )
        result = composite_score(panel, [f])
        assert result.shape == panel["close"].shape
        assert list(result.index) == list(panel["close"].index)
        assert list(result.columns) == list(panel["close"].columns)

    def test_negative_ic_factor_flips_sign(self):
        """A factor with negative IC should contribute the opposite sign to composite.

        Setup: one 'signal' factor based on `close` with IC = +0.5, and one
        factor based on the identical signal but IC = -0.5.  The composites of
        each factor alone should be exact sign-inversions of each other.
        """
        panel = self._make_panel(seed=1)

        pos_factor = FactorsLibrary(
            name="pos_ic",
            formula_json=json.dumps({"formula": "rank(close)", "dsl": "rank(close)", "name": "pos_ic"}),
            source="test",
            ic_summary_json=json.dumps({"ic": 0.5, "icir": 2.0, "ic_series": []}),
        )
        neg_factor = FactorsLibrary(
            name="neg_ic",
            formula_json=json.dumps({"formula": "rank(close)", "dsl": "rank(close)", "name": "neg_ic"}),
            source="test",
            ic_summary_json=json.dumps({"ic": -0.5, "icir": -2.0, "ic_series": []}),
        )

        comp_pos = composite_score(panel, [pos_factor])
        comp_neg = composite_score(panel, [neg_factor])

        # The two composites should be exactly negatives of each other.
        diff = (comp_pos + comp_neg).abs()
        assert diff.max().max() < 1e-10, (
            "Negative-IC factor should produce exactly opposite composite to positive-IC factor"
        )

    def test_empty_factors_list_returns_empty_dataframe(self):
        panel = self._make_panel()
        result = composite_score(panel, [])
        assert result.empty or result.isna().all().all()

    def test_invalid_factor_is_skipped(self):
        """A factor with an invalid DSL formula should be skipped, not crash."""
        panel = self._make_panel()
        bad_factor = FactorsLibrary(
            name="bad",
            formula_json=json.dumps({"formula": "INVALID_EXPR", "dsl": "INVALID_EXPR!!!", "name": "bad"}),
            source="test",
            ic_summary_json=json.dumps({"ic": 0.5, "icir": 1.0, "ic_series": []}),
        )
        # Should not raise; returns empty composite (only invalid factor).
        result = composite_score(panel, [bad_factor])
        assert isinstance(result, pd.DataFrame)

    def test_two_equal_magnitude_factors_average(self):
        """Two factors with the same signal and same |IC| should average to same magnitude."""
        panel = self._make_panel(seed=3)
        f_single = FactorsLibrary(
            name="single",
            formula_json=json.dumps({"formula": "rank(close)", "dsl": "rank(close)", "name": "single"}),
            source="test",
            ic_summary_json=json.dumps({"ic": 0.4, "icir": 2.0, "ic_series": []}),
        )
        f_double1 = FactorsLibrary(
            name="d1",
            formula_json=json.dumps({"formula": "rank(close)", "dsl": "rank(close)", "name": "d1"}),
            source="test",
            ic_summary_json=json.dumps({"ic": 0.4, "icir": 2.0, "ic_series": []}),
        )
        f_double2 = FactorsLibrary(
            name="d2",
            formula_json=json.dumps({"formula": "rank(close)", "dsl": "rank(close)", "name": "d2"}),
            source="test",
            ic_summary_json=json.dumps({"ic": 0.4, "icir": 2.0, "ic_series": []}),
        )
        comp_single = composite_score(panel, [f_single])
        comp_double = composite_score(panel, [f_double1, f_double2])
        # averaging identical frames should yield the same result
        diff = (comp_single - comp_double).abs()
        assert diff.max().max() < 1e-10


# ---------------------------------------------------------------------------
# 3. build_weights
# ---------------------------------------------------------------------------


class TestBuildWeights:
    def _make_composite(self, n_dates: int = 30, n_symbols: int = 6) -> pd.DataFrame:
        rng = np.random.default_rng(99)
        idx = pd.date_range("2024-01-02", periods=n_dates, freq="B")
        cols = [f"A{i}" for i in range(n_symbols)]
        return pd.DataFrame(rng.standard_normal((n_dates, n_symbols)), index=idx, columns=cols)

    def test_long_only_all_weights_nonneg(self):
        composite = self._make_composite()
        config = TraderConfig(rebalance_freq="D", position_size=0.1, commission=0.001, max_positions=3)
        weights = build_weights(composite, config)
        assert (weights >= 0).all().all(), "Weights must be non-negative (long-only)"

    def test_gross_exposure_capped_at_one(self):
        composite = self._make_composite()
        config = TraderConfig(rebalance_freq="D", position_size=0.5, commission=0.001, max_positions=3)
        weights = build_weights(composite, config)
        gross = weights.sum(axis=1)
        assert (gross <= 1.0 + 1e-9).all(), f"Gross > 1 on some rows: {gross[gross > 1.0 + 1e-9]}"

    def test_at_most_max_positions_nonzero_per_row(self):
        composite = self._make_composite(n_symbols=8)
        config = TraderConfig(rebalance_freq="D", position_size=0.1, commission=0.001, max_positions=3)
        weights = build_weights(composite, config)
        nonzero_per_row = (weights > 1e-12).sum(axis=1)
        assert (nonzero_per_row <= config.max_positions).all(), (
            f"Some rows have more than {config.max_positions} positions"
        )

    def test_top_composite_symbol_is_selected(self):
        """The symbol with the highest composite score on a daily rebalance date
        must receive a positive weight."""
        idx = pd.date_range("2024-01-02", periods=5, freq="D")
        # X always has the highest score.
        composite = pd.DataFrame(
            {"X": [3.0, 3.0, 3.0, 3.0, 3.0], "Y": [1.0, 1.0, 1.0, 1.0, 1.0], "Z": [-1.0, -1.0, -1.0, -1.0, -1.0]},
            index=idx,
        )
        config = TraderConfig(rebalance_freq="D", position_size=0.2, commission=0.001, max_positions=1)
        weights = build_weights(composite, config)
        assert (weights["X"] > 0).all(), "Top-scoring symbol X should always have positive weight"
        assert (weights["Y"] == 0).all(), "Lower-scoring Y should have zero weight with max_positions=1"

    def test_weights_forward_fill_between_weekly_rebalances(self):
        """Between weekly rebalance dates, weights should be constant (forward-filled)."""
        # 10 business days = 2 full weeks; rebalance on W means ~2 rebalance points.
        idx = pd.date_range("2024-01-02", periods=10, freq="B")
        composite = pd.DataFrame(
            {"P": [2.0] * 10, "Q": [1.0] * 10},
            index=idx,
        )
        config = TraderConfig(rebalance_freq="W", position_size=0.3, commission=0.001, max_positions=1)
        weights = build_weights(composite, config)
        # All rows should be non-NaN (ffill must have filled them).
        assert not weights.isna().any().any(), "Weights should not contain NaN after ffill"
        # Rows between rebalances should carry the same weights as the last rebalance.
        # The whole frame should have only one unique value per column (no drift).
        for col in weights.columns:
            unique_vals = weights[col].unique()
            assert len(unique_vals) <= 2, f"Too many unique weight values for {col}: {unique_vals}"


# ---------------------------------------------------------------------------
# 4. strategy_equity
# ---------------------------------------------------------------------------


class TestStrategyEquity:
    def test_deterministic_two_asset_case(self):
        """Hand-computed equity for a 2-day, 2-asset portfolio.

        Prices:
            A: 100 -> 110 -> 121   (pct_change filled: 0, +10%, +10%)
            B: 100 -> 90  -> 99    (pct_change filled: 0,  -10%, +10%)
        Weights (constant 0.5/0.5 every day):
            day 0: A=0.5, B=0.5
            day 1: A=0.5, B=0.5
            day 2: A=0.5, B=0.5

        weights.shift(1) — NaN on day 0; pandas .sum(axis=1) treats NaN*value as 0:
            day 0 port_ret = 0 (NaN weights → 0 contribution)
            day 1 port_ret = 0.5*0.10 + 0.5*(-0.10) = ~0.0
            day 2 port_ret = 0.5*0.10 + 0.5*0.10 = 0.10

        turnover = weights.diff().abs().sum(axis=1).fillna(0):
            Constant weights → diff = 0 everywhere → turnover = 0 (no commission charged).

        net = port_ret - 0*commission → same as port_ret.

        equity (initial=1000):
            day 0: 1.0 * 1000 = 1000.0
            day 1: 1000 * (1 + 0.0) ≈ 1000.0
            day 2: 1000 * (1.0) * 1.10 = 1100.0
        """
        idx = pd.date_range("2024-01-01", periods=3, freq="D")
        close_df = pd.DataFrame(
            {"A": [100.0, 110.0, 121.0], "B": [100.0, 90.0, 99.0]},
            index=idx,
        )
        weights = pd.DataFrame(
            {"A": [0.5, 0.5, 0.5], "B": [0.5, 0.5, 0.5]},
            index=idx,
        )
        equity = strategy_equity(close_df, weights, commission=0.001, initial_cash=1000.0)

        assert math.isclose(equity.iloc[0], 1000.0, rel_tol=1e-9), f"Day 0 equity: {equity.iloc[0]}"
        # Day 1: net port_ret ≈ 0 (cancelling +10%/-10%) → equity unchanged.
        assert math.isclose(equity.iloc[1], 1000.0, rel_tol=1e-6), f"Day 1 equity: {equity.iloc[1]}"
        # Day 2: both assets +10% → +10% gross, zero turnover cost → 1100.
        assert math.isclose(equity.iloc[2], 1100.0, rel_tol=1e-6), f"Day 2 equity: {equity.iloc[2]}"

    def test_deterministic_two_asset_with_turnover(self):
        """Hand-compute case that actually generates turnover so commission is charged.

        Prices: A: 100->110->121, B: 100->90->99 (same as above).
        Weights: day 0: A=0.8, B=0.0; day 1: A=0.0, B=0.8; day 2: A=0.0, B=0.8

        weights.shift(1):
            day 0: NaN/NaN → port_ret = 0
            day 1: A=0.8, B=0.0
            day 2: A=0.0, B=0.8

        pct_change (filled): day0=(0,0), day1=(+0.10,-0.10), day2=(+0.10,+0.10)

        port_ret:
            day 0: 0
            day 1: 0.8*0.10 + 0*(-0.10) = 0.08
            day 2: 0*0.10 + 0.8*0.10 = 0.08

        turnover = weights.diff().abs().sum(axis=1).fillna(0):
            day 0: fillna → 0
            day 1: |0-0.8| + |0.8-0| = 1.6
            day 2: |0-0| + |0.8-0.8| = 0.0

        commission = 0.01 (deliberately high to make the effect visible)
        net:
            day 0: 0
            day 1: 0.08 - 1.6*0.01 = 0.08 - 0.016 = 0.064
            day 2: 0.08 - 0.0 = 0.08

        equity (initial=1000):
            day 0: 1000.0
            day 1: 1000 * 1.064 = 1064.0
            day 2: 1064 * 1.08 = 1149.12
        """
        idx = pd.date_range("2024-01-01", periods=3, freq="D")
        close_df = pd.DataFrame(
            {"A": [100.0, 110.0, 121.0], "B": [100.0, 90.0, 99.0]},
            index=idx,
        )
        weights = pd.DataFrame(
            {"A": [0.8, 0.0, 0.0], "B": [0.0, 0.8, 0.8]},
            index=idx,
        )
        equity = strategy_equity(close_df, weights, commission=0.01, initial_cash=1000.0)

        assert math.isclose(equity.iloc[0], 1000.0, rel_tol=1e-9), f"Day 0: {equity.iloc[0]}"
        assert math.isclose(equity.iloc[1], 1064.0, rel_tol=1e-6), f"Day 1: {equity.iloc[1]}"
        assert math.isclose(equity.iloc[2], 1149.12, rel_tol=1e-6), f"Day 2: {equity.iloc[2]}"

    def test_higher_commission_yields_lower_terminal_equity(self):
        """Given the same turnover, higher commission must produce lower final equity."""
        idx = pd.date_range("2024-01-01", periods=10, freq="D")
        rng = np.random.default_rng(20)
        prices = pd.DataFrame(
            100 * np.exp(rng.standard_normal((10, 3)).cumsum(axis=0) * 0.01),
            index=idx,
            columns=["X", "Y", "Z"],
        )
        # Weights that change every day → high turnover.
        w = pd.DataFrame(rng.dirichlet(np.ones(3), size=10) * 0.9, index=idx, columns=["X", "Y", "Z"])
        eq_low = strategy_equity(prices, w, commission=0.0001, initial_cash=1000.0)
        eq_high = strategy_equity(prices, w, commission=0.01, initial_cash=1000.0)
        assert eq_high.iloc[-1] < eq_low.iloc[-1], (
            "Higher commission should yield lower terminal equity when there is turnover"
        )

    def test_zero_weights_returns_flat_equity(self):
        """All-zero weights produce no portfolio return: equity stays at initial_cash."""
        idx = pd.date_range("2024-01-01", periods=5, freq="D")
        prices = pd.DataFrame(
            {"A": [100.0, 110.0, 120.0, 130.0, 140.0], "B": [100.0, 90.0, 80.0, 70.0, 60.0]},
            index=idx,
        )
        weights = pd.DataFrame(0.0, index=idx, columns=["A", "B"])
        equity = strategy_equity(prices, weights, commission=0.001, initial_cash=5000.0)
        # No positions → equity should remain at initial_cash throughout.
        assert (equity == 5000.0).all(), f"Non-flat equity with zero weights: {equity.values}"


# ---------------------------------------------------------------------------
# 5. _rebalance_dates
# ---------------------------------------------------------------------------


class TestRebalanceDates:
    def _bdays(self, n: int) -> pd.DatetimeIndex:
        return pd.bdate_range("2024-01-02", periods=n)

    def test_daily_returns_all_dates(self):
        idx = self._bdays(20)
        dates = _rebalance_dates(idx, "D")
        assert len(dates) == len(idx)
        assert set(dates) == set(idx)

    def test_weekly_returns_fewer_than_daily(self):
        idx = self._bdays(60)  # ~12 weeks
        weekly = _rebalance_dates(idx, "W")
        assert len(weekly) < len(idx), "Weekly should have fewer dates than daily"
        assert len(weekly) >= 1

    def test_monthly_returns_fewer_than_weekly(self):
        idx = self._bdays(252)  # ~1 year
        weekly = _rebalance_dates(idx, "W")
        monthly = _rebalance_dates(idx, "M")
        assert len(monthly) < len(weekly), "Monthly should have fewer dates than weekly"

    def test_empty_index_returns_empty_list(self):
        idx = pd.DatetimeIndex([])
        assert _rebalance_dates(idx, "D") == []
        assert _rebalance_dates(idx, "W") == []
        assert _rebalance_dates(idx, "M") == []

    def test_rebalance_dates_are_subset_of_index(self):
        idx = self._bdays(30)
        for freq in ("D", "W", "M"):
            dates = _rebalance_dates(idx, freq)
            assert all(d in idx for d in dates), f"Rebalance dates for {freq} not in original index"


# ---------------------------------------------------------------------------
# Stub registry: satisfies the _Registry protocol without network calls.
# ---------------------------------------------------------------------------


class _NoopRegistry:
    """Returns empty fundamentals for all symbols — no provider calls made."""

    def get_fundamentals(self, symbol: str) -> dict:  # noqa: ARG002
        return {}


_NOOP_REGISTRY = _NoopRegistry()


# ---------------------------------------------------------------------------
# 6. End-to-end run_trader
# ---------------------------------------------------------------------------


class TestRunTrader:
    """Slow test: imports vectorbt + real DB round-trip.  Allow several minutes."""

    def test_run_trader_returns_results_and_persists(self):
        db = _memory_db()
        symbols = _seed_trend_bars(db, n_symbols=5, days=90)
        f1, f2, screener_run = _seed_factors_and_screener(db, symbols)

        now = datetime.now(UTC)
        start = now - timedelta(days=92)
        end = now - timedelta(days=2)

        results = asyncio.run(
            run_trader(db, screener_run.id, symbols, start, end, num_configs=5, registry=_NOOP_REGISTRY)
        )

        # Non-empty results.
        assert len(results) >= 1, "run_trader returned no results"

        # Every result has a finite sharpe_ratio.
        for r in results:
            assert isinstance(r, TraderResult)
            assert math.isfinite(r.sharpe_ratio), f"Non-finite sharpe_ratio: {r.sharpe_ratio}"

        # Results sorted by sharpe descending.
        sharpes = [r.sharpe_ratio for r in results]
        assert sharpes == sorted(sharpes, reverse=True), "Results not sorted by sharpe descending"

        # Ranks are 1..n.
        ranks = [r.rank for r in results]
        assert ranks == list(range(1, len(results) + 1)), f"Ranks are not 1..n: {ranks}"

        # TraderBacktest rows were persisted.
        rows = db.execute(
            select(TraderBacktest).where(TraderBacktest.screener_run_id == screener_run.id)
        ).scalars().all()
        assert len(rows) >= 1, "No TraderBacktest rows were persisted"

        # result_json contains parseable sharpe_ratio for every row.
        for row in rows:
            parsed = json.loads(row.result_json)
            assert "sharpe_ratio" in parsed, f"result_json missing sharpe_ratio: {row.result_json}"
            assert isinstance(parsed["sharpe_ratio"], (int, float))

    def test_run_trader_walk_forward_gate_runs_and_is_recorded(self):
        """Track C1: run_trader wires walk_forward_test in as a real
        pre-selection gate. With a long (>=378-day, enough for 2 walk-forward
        folds) panel of consistently trending symbols, the gate should have
        enough data to actually run (periods_tested >= 2 — a single fold
        can't produce a non-degenerate Sharpe) and a config whose composite
        ranking stays stable across time should clear it (present in
        results, wfe recorded on the persisted row) — proving the gate is
        wired, not a dead no-op."""
        db = _memory_db()
        # Fixed anchor, not now(): where month-end rebalances fall relative
        # to the walk-forward fold boundaries shifts with the calendar, and on
        # ~1-2 days a month (anchors 2026-08-26, 2026-09-25 of 60 scanned) the
        # WFE of this synthetic panel dips below the 0.5 gate -- a
        # clock-dependent flake, not a regression.
        now = _WALK_FORWARD_ANCHOR
        symbols = _seed_trend_bars(db, n_symbols=5, days=450, anchor=now)
        f1, f2, screener_run = _seed_factors_and_screener(db, symbols)

        start = now - timedelta(days=452)
        end = now - timedelta(days=2)

        custom_settings = {
            "alphacrafter_rebalance_freqs": ["M"],
            "alphacrafter_position_sizes": [0.1],
            "alphacrafter_commissions": [0.001],
        }
        with patch(
            "app.lab.alphacrafter.trader.get_public_settings",
            return_value=custom_settings,
        ):
            results = asyncio.run(
                run_trader(
                    db, screener_run.id, symbols, start, end,
                    num_configs=5, registry=_NOOP_REGISTRY,
                )
            )

        assert len(results) >= 1, (
            "a stably-trending config should clear the walk-forward gate, "
            "not be rejected"
        )

        rows = db.execute(
            select(TraderBacktest).where(TraderBacktest.screener_run_id == screener_run.id)
        ).scalars().all()
        assert len(rows) >= 1
        gated_rows = [json.loads(r.result_json) for r in rows]
        assert any(r.get("walk_forward_periods_tested", 0) >= 2 for r in gated_rows), (
            "walk-forward gate never actually ran on a long enough panel — "
            f"periods_tested values: {[r.get('walk_forward_periods_tested') for r in gated_rows]}"
        )
        ran = [r for r in gated_rows if r.get("walk_forward_periods_tested", 0) >= 2]
        for r in ran:
            assert r.get("walk_forward_wfe") is not None
            assert r["walk_forward_wfe"] >= 0.5, (
                "a persisted (i.e. non-rejected) config must have cleared the WFE gate"
            )

    def test_run_trader_is_deterministic(self):
        """Calling run_trader twice on identical data must produce identical sharpe lists."""
        db = _memory_db()
        symbols = _seed_trend_bars(db, n_symbols=5, days=90)
        _f1, _f2, screener_run = _seed_factors_and_screener(db, symbols)

        now = datetime.now(UTC)
        start = now - timedelta(days=92)
        end = now - timedelta(days=2)

        results1 = asyncio.run(
            run_trader(db, screener_run.id, symbols, start, end, num_configs=5, registry=_NOOP_REGISTRY)
        )
        results2 = asyncio.run(
            run_trader(db, screener_run.id, symbols, start, end, num_configs=5, registry=_NOOP_REGISTRY)
        )

        sharpes1 = [r.sharpe_ratio for r in results1]
        sharpes2 = [r.sharpe_ratio for r in results2]
        assert sharpes1 == sharpes2, (
            f"run_trader is not deterministic: first={sharpes1}, second={sharpes2}"
        )

    def test_run_trader_empty_on_no_factors(self):
        """A ScreenerRun with no selected factors returns an empty list."""
        db = _memory_db()
        symbols = _seed_trend_bars(db, n_symbols=3, days=30)
        screener_run = ScreenerRun(
            regime_label=None,
            selected_factor_ids="",
            scores_json="{}",
        )
        db.add(screener_run)
        db.commit()

        now = datetime.now(UTC)
        results = asyncio.run(
            run_trader(
                db, screener_run.id, symbols,
                now - timedelta(days=32), now,
                num_configs=5,
                registry=_NOOP_REGISTRY,
            )
        )
        assert results == [], "Expected empty list when no factors are selected"


# ---------------------------------------------------------------------------
# 7. TraderAgent SharedMemoryH integration
# ---------------------------------------------------------------------------


class TestTraderAgent:
    """Tests for TraderAgent using SharedMemoryH."""

    def _make_h(
        self,
        universe: list[str],
        start_date: str,
        end_date: str,
        screener_outputs: dict | None = None,
        factor_states: list | None = None,
    ) -> SharedMemoryH:
        """Create a SharedMemoryH for testing."""
        h = SharedMemoryH.create_initial(universe, start_date, end_date)
        if screener_outputs:
            h.screener_outputs = screener_outputs
        if factor_states:
            h.factor_states = factor_states
        return h

    def test_trader_writes_to_h_trader_outputs(self):
        """TraderAgent should write results to H.trader_outputs."""
        db = _memory_db()
        symbols = _seed_trend_bars(db, n_symbols=5, days=90)
        f1, f2, screener_run = _seed_factors_and_screener(db, symbols)

        now = datetime.now(UTC)
        start = now - timedelta(days=92)
        end = now - timedelta(days=2)

        h = self._make_h(
            universe=symbols,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            screener_outputs={
                "selected_factor_ids": f"{f1.id},{f2.id}",
                "screener_run_id": str(screener_run.id),
            },
        )

        agent = TraderAgent()
        result_h = asyncio.run(agent.run(h, db))

        assert "trader_outputs" in result_h.__dict__, "trader_outputs not in H"
        assert result_h.trader_outputs != {}, "trader_outputs should not be empty"

    def test_trader_reads_selected_factor_ids_from_h(self):
        """TraderAgent should read selected_factor_ids from H.screener_outputs."""
        db = _memory_db()
        symbols = _seed_trend_bars(db, n_symbols=5, days=90)
        f1, f2, screener_run = _seed_factors_and_screener(db, symbols)

        now = datetime.now(UTC)
        start = now - timedelta(days=92)
        end = now - timedelta(days=2)

        selected_ids = f"{f1.id},{f2.id}"
        h = self._make_h(
            universe=symbols,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            screener_outputs={
                "selected_factor_ids": selected_ids,
                "screener_run_id": str(screener_run.id),
            },
        )

        agent = TraderAgent()
        result_h = asyncio.run(agent.run(h, db))

        assert "n_factors" in result_h.trader_outputs, "n_factors not in trader_outputs"
        assert result_h.trader_outputs["n_factors"] >= 1, "Should have at least 1 factor"

    def test_trader_uses_configurable_grid_from_settings(self):
        """TraderAgent should read grid config from settings."""
        db = _memory_db()
        symbols = _seed_trend_bars(db, n_symbols=5, days=90)
        f1, f2, screener_run = _seed_factors_and_screener(db, symbols)

        now = datetime.now(UTC)
        start = now - timedelta(days=92)
        end = now - timedelta(days=2)

        h = self._make_h(
            universe=symbols,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            screener_outputs={
                "selected_factor_ids": f"{f1.id},{f2.id}",
                "screener_run_id": str(screener_run.id),
            },
        )

        custom_settings = {
            "alphacrafter_rebalance_freqs": ["M"],
            "alphacrafter_position_sizes": [0.1],
            "alphacrafter_commissions": [0.001],
        }

        agent = TraderAgent()
        with patch(
            "app.lab.alphacrafter.trader.get_public_settings",
            return_value=custom_settings,
        ):
            result_h = asyncio.run(agent.run(h, db))

        assert "trader_outputs" in result_h.__dict__, "trader_outputs not in H"
        assert result_h.trader_outputs != {}, "trader_outputs should not be empty"

    def test_trader_appends_to_agent_history(self):
        """TraderAgent should append action to H.agent_history."""
        db = _memory_db()
        symbols = _seed_trend_bars(db, n_symbols=5, days=90)
        f1, f2, screener_run = _seed_factors_and_screener(db, symbols)

        now = datetime.now(UTC)
        start = now - timedelta(days=92)
        end = now - timedelta(days=2)

        h = self._make_h(
            universe=symbols,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            screener_outputs={
                "selected_factor_ids": f"{f1.id},{f2.id}",
                "screener_run_id": str(screener_run.id),
            },
        )

        initial_history_len = len(h.agent_history)

        agent = TraderAgent()
        result_h = asyncio.run(agent.run(h, db))

        assert len(result_h.agent_history) > initial_history_len, (
            "agent_history should have been appended"
        )
        last_entry = result_h.agent_history[-1]
        assert last_entry["agent"] == "trader", f"Expected agent='trader', got {last_entry['agent']}"

    def test_trader_empty_when_no_selected_factors(self):
        """TraderAgent should return empty trader_outputs when no factors selected."""
        db = _memory_db()
        symbols = _seed_trend_bars(db, n_symbols=3, days=30)

        now = datetime.now(UTC)
        start = now - timedelta(days=32)
        end = now

        h = self._make_h(
            universe=symbols,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            screener_outputs={
                "selected_factor_ids": "",
                "screener_run_id": "none",
            },
        )

        agent = TraderAgent()
        result_h = asyncio.run(agent.run(h, db))

        assert result_h.trader_outputs.get("n_configs_run", 0) == 0, (
            "Should have 0 configs run when no factors selected"
        )


# ---------------------------------------------------------------------------
# 8. Settings-driven grid parsing (audit-fixes-2026-08 todo 18)
# ---------------------------------------------------------------------------


class TestTraderGridSettingsParsing:
    """CSV/int settings strings parse into the trader grid; malformed → default."""

    def test_csv_freq_string_parses(self):
        freqs, sizes, comms, max_pos = _trader_grid_settings({
            "alphacrafter_rebalance_freqs": "W,M",
            "alphacrafter_position_sizes": "0.05,0.1,0.2",
            "alphacrafter_commissions": "0.001",
            "alphacrafter_max_positions": "5",
        })
        assert freqs == ["W", "M"]
        assert sizes == [0.05, 0.1, 0.2]
        assert comms == [0.001]
        assert max_pos == 5

    def test_defaults_equal_legacy_fallbacks(self):
        """Empty settings dict resolves to EXACTLY the pre-todo-18 fallbacks."""
        freqs, sizes, comms, max_pos = _trader_grid_settings({})
        assert freqs == DEFAULT_REBALANCE_FREQS == ["W", "M"]
        assert sizes == DEFAULT_POSITION_SIZES == [0.05, 0.1, 0.2]
        assert comms == DEFAULT_COMMISSIONS == [0.001]
        assert max_pos == DEFAULT_MAX_POSITIONS == 5

    def test_malformed_values_fall_back_with_warning(self, caplog):
        import logging as _logging

        with caplog.at_level(_logging.WARNING, logger="app.lab.alphacrafter.trader"):
            freqs, sizes, comms, max_pos = _trader_grid_settings({
                "alphacrafter_rebalance_freqs": ",,,",
                "alphacrafter_position_sizes": "abc,def",
                "alphacrafter_commissions": "",
                "alphacrafter_max_positions": "not-a-number",
            })
        assert freqs == DEFAULT_REBALANCE_FREQS
        assert sizes == DEFAULT_POSITION_SIZES
        assert comms == DEFAULT_COMMISSIONS
        assert max_pos == DEFAULT_MAX_POSITIONS
        assert "falling back" in caplog.text or "fallback" in caplog.text, (
            f"Expected fallback warnings, got: {caplog.records}"
        )

    def test_legacy_list_values_still_accepted(self):
        """Pre-18 stored values were JSON lists — they must keep working."""
        freqs, sizes, comms, max_pos = _trader_grid_settings({
            "alphacrafter_rebalance_freqs": ["M"],
            "alphacrafter_position_sizes": [0.1],
            "alphacrafter_commissions": [0.001],
            "alphacrafter_max_positions": 7,
        })
        assert freqs == ["M"]
        assert sizes == [0.1]
        assert comms == [0.001]
        assert max_pos == 7

    def test_settings_driven_grid_equals_old_hardcoded_triple_under_defaults(self):
        """Under default settings the generated grid is IDENTICAL to the old
        hardcoded triple (2 freqs × 3 sizes × 1 commission, max_positions=5)."""
        old_hardcoded = [
            TraderConfig(rebalance_freq=freq, position_size=size, commission=0.001)
            for freq in ["W", "M"]
            for size in [0.05, 0.1, 0.2]
        ]
        freqs, sizes, comms, max_pos = _trader_grid_settings({})
        new_grid = _generate_config_grid(freqs, sizes, comms, max_positions=max_pos)
        assert [vars(c) for c in new_grid] == [vars(c) for c in old_hardcoded], (
            "Settings-driven grid must equal the legacy hardcoded grid under defaults"
        )
        assert len(new_grid) == 6

    def test_setting_change_alters_grid_specs(self):
        """commissions='0.002' must flow into generated config specs."""
        _, _, comms, _ = _trader_grid_settings({"alphacrafter_commissions": "0.002"})
        grid = _generate_config_grid(["W"], [0.1], comms)
        assert all(c.commission == 0.002 for c in grid)

    def test_max_positions_setting_feeds_generated_configs(self):
        _, _, _, max_pos = _trader_grid_settings({"alphacrafter_max_positions": "8"})
        grid = _generate_config_grid(["W"], [0.1], [0.001], max_positions=max_pos)
        assert all(c.max_positions == 8 for c in grid)

    def test_nonpositive_max_positions_falls_back(self):
        _, _, _, max_pos = _trader_grid_settings({"alphacrafter_max_positions": "0"})
        assert max_pos == DEFAULT_MAX_POSITIONS
