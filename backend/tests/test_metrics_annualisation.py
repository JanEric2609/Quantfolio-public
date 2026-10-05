"""Golden-output tests for the three metrics-bundle builders (plan todo 30).

CAPTURE-FIRST PROVENANCE
------------------------
Every golden value below was captured from the *unmodified* implementations on
branch ``audit/full-audit-2026-08`` at commit ``bcc3cfe`` (2026-08-24), BEFORE
the explicit ``periods_per_year`` parameter was introduced. The post-refactor
suite must reproduce them bit-for-bit.

Governing convention: ``docs/adr/0003-quant-metrics-conventions.md`` decision 4 —

| Builder                          | Cadence             | Default constant              |
|----------------------------------|---------------------|-------------------------------|
| backtest_vbt/metrics.compute_metrics | trading days    | TRADING_DAYS_PER_YEAR (252)   |
| paper_portfolio.compute_metrics      | calendar days   | CALENDAR_DAYS_PER_YEAR (365)  |

Parameterize, never flatten: defaults preserve each caller's historical
numbers exactly; passing an explicit value must visibly move annualised
outputs; non-positive values must fail fast with ValueError.
"""

import inspect
import math
from dataclasses import asdict
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import PaperPortfolio, PaperSnapshot
from app.foundation import quant_metrics
from app.lab.backtest_vbt.metrics import BacktestMetrics, compute_metrics as vbt_compute_metrics
from app.decision.paper_portfolio import compute_metrics as pp_compute_metrics


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


# ---------------------------------------------------------------------------
# Deterministic fixtures — MUST stay byte-identical to the capture run
# (np.random.default_rng streams and the formulas below were frozen at
# capture time; changing either invalidates every golden in this file).
# ---------------------------------------------------------------------------


def _vbt_fixture() -> tuple[np.ndarray, np.ndarray, list[dict]]:
    rng = np.random.default_rng(42)
    rets = rng.normal(0.0005, 0.01, size=99)
    equity = 100_000.0 * np.cumprod(np.concatenate([[1.0], 1.0 + rets]))
    timestamps = pd.date_range("2024-01-02", periods=100, freq="B").to_numpy()
    trades = [{"pnl": 120.0}, {"pnl": -50.0}, {"pnl": 80.0}, {"pnl": -30.0}, {"pnl": 200.0}]
    return equity, timestamps, trades


def _seed_paper_snapshots(db, portfolio_id: str, n: int = 25) -> None:
    """Same deterministic series the goldens were captured from."""
    snap_values = [100000.0 * (1.0 + 0.002 * i + 0.01 * math.sin(i)) for i in range(n)]
    today = date.today()
    for i, val in enumerate(snap_values):
        snap_date = today - timedelta(days=(n - 1 - i))
        db.add(
            PaperSnapshot(
                portfolio_id=portfolio_id,
                date=snap_date,
                total_value=round(val, 2),
                cash_balance=50000.0,
                securities_value=round(val - 50000.0, 2),
                total_return_pct=round((val - 100000.0) / 100000.0, 6),
                currency="EUR",
            )
        )
    db.commit()


def _seed_paper_portfolio(db, user_id: str = "user-1") -> PaperPortfolio:
    portfolio = PaperPortfolio(
        user_id=user_id,
        name="Manual Baseline",
        currency="EUR",
        initial_cash=100000,
        baseline_value=100000,
        mandate="manual",
        managed_by="manual",
    )
    db.add(portfolio)
    db.commit()
    return portfolio


# ---------------------------------------------------------------------------
# Golden outputs — backtest_vbt/metrics.py:compute_metrics (trading days, 252)
# ---------------------------------------------------------------------------


class TestGoldenBacktestVbtMetrics:
    def test_with_trades_matches_captured_golden(self):
        equity, timestamps, trades = _vbt_fixture()

        metrics = vbt_compute_metrics(equity, timestamps, trades)

        assert isinstance(metrics, BacktestMetrics)
        assert metrics.sharpe_ratio == pytest.approx(0.17124774491710462, rel=1e-6)
        assert metrics.sortino_ratio == pytest.approx(0.23788941913273637, rel=1e-6)
        assert metrics.calmar_ratio == pytest.approx(0.1811821266611038, rel=1e-6)
        assert metrics.max_drawdown == pytest.approx(-0.07544404539444433, rel=1e-6)
        # Longest consecutive underwater streak (peak-to-peak), NOT the count of
        # drawdown episodes. 1da66d9 delegated this to quant_metrics.max_drawdown
        # (ADR 0003 decision 4); the old golden 6 encoded the retired episode-count.
        assert metrics.drawdown_duration_days == 47
        assert metrics.total_return == pytest.approx(0.005347887121090171, rel=1e-6)
        # CAGR exponent uses len(returns) == len(equity) - 1 periods (1da66d9
        # off-by-one fix); the old golden divided by len(equity).
        assert metrics.annual_return == pytest.approx(0.013669112588480026, rel=1e-6)
        assert metrics.win_rate == pytest.approx(0.6, rel=1e-6)
        assert metrics.profit_factor == pytest.approx(5.0, rel=1e-6)
        assert metrics.return_std == pytest.approx(0.007718582572311831, rel=1e-6)

    def test_without_trades_matches_captured_golden(self):
        equity, timestamps, _ = _vbt_fixture()

        metrics = vbt_compute_metrics(equity, timestamps, None)

        assert metrics.sharpe_ratio == pytest.approx(0.17124774491710462, rel=1e-6)
        assert metrics.sortino_ratio == pytest.approx(0.23788941913273637, rel=1e-6)
        assert metrics.calmar_ratio == pytest.approx(0.1811821266611038, rel=1e-6)
        assert metrics.max_drawdown == pytest.approx(-0.07544404539444433, rel=1e-6)
        # Longest consecutive underwater streak (peak-to-peak), NOT the count of
        # drawdown episodes. 1da66d9 delegated this to quant_metrics.max_drawdown
        # (ADR 0003 decision 4); the old golden 6 encoded the retired episode-count.
        assert metrics.drawdown_duration_days == 47
        assert metrics.total_return == pytest.approx(0.005347887121090171, rel=1e-6)
        # CAGR exponent uses len(returns) == len(equity) - 1 periods (1da66d9
        # off-by-one fix); the old golden divided by len(equity).
        assert metrics.annual_return == pytest.approx(0.013669112588480026, rel=1e-6)
        # Win rate falls back to per-period returns when no trades are given.
        assert metrics.win_rate == 0.5252525252525253
        assert metrics.profit_factor == 1.0
        assert metrics.return_std == 0.007718582572311831

    def test_empty_equity_curve_zeroed_bundle(self):
        metrics = vbt_compute_metrics(np.array([]), np.array([], dtype="datetime64[ns]"))

        assert asdict(metrics) == {
            "sharpe_ratio": 0.0,
            "sortino_ratio": 0.0,
            "calmar_ratio": 0.0,
            "max_drawdown": 0.0,
            "drawdown_duration_days": 0,
            "total_return": 0.0,
            "annual_return": 0.0,
            "win_rate": 0.0,
            "profit_factor": 1.0,
            "return_std": 0.0,
        }


# ---------------------------------------------------------------------------
# Golden outputs — paper_portfolio.py:compute_metrics (calendar days, 365)
# ---------------------------------------------------------------------------


class TestGoldenPaperPortfolioMetrics:
    def test_snapshot_history_matches_captured_golden(self):
        db = _memory_db()
        portfolio = _seed_paper_portfolio(db)
        _seed_paper_snapshots(db, portfolio.id)

        result = pp_compute_metrics(db, portfolio.id)

        assert result == {
            "updated": True,
            "sharpe": 4.427753824016444,
            "max_drawdown": -0.01354282744331381,
            "samples": 24,
        }

    def test_insufficient_history_branch(self):
        db = _memory_db()
        portfolio = _seed_paper_portfolio(db, user_id="user-thin")

        result = pp_compute_metrics(db, portfolio.id)

        assert result == {
            "updated": False,
            "reason": "insufficient snapshot history",
            "samples": 0,
        }


# ---------------------------------------------------------------------------
# ADR decision 4: each builder's DEFAULT is its named cadence constant
# ---------------------------------------------------------------------------


class TestDefaultsMatchAdrConventionTable:
    def test_backtest_vbt_default_is_trading_days(self):
        default = inspect.signature(vbt_compute_metrics).parameters["periods_per_year"].default
        assert default == quant_metrics.TRADING_DAYS_PER_YEAR == 252

    def test_paper_portfolio_default_is_calendar_days(self):
        default = inspect.signature(pp_compute_metrics).parameters["periods_per_year"].default
        assert default == quant_metrics.CALENDAR_DAYS_PER_YEAR == 365


# ---------------------------------------------------------------------------
# The parameter is live: explicit values thread through and move numbers
# ---------------------------------------------------------------------------


class TestPeriodsPerYearThreadsThrough:
    def test_vbt_explicit_constant_equals_default_and_nondefault_moves_numbers(self):
        equity, timestamps, trades = _vbt_fixture()

        default = vbt_compute_metrics(equity, timestamps, trades)
        explicit_const = vbt_compute_metrics(
            equity, timestamps, trades, periods_per_year=quant_metrics.TRADING_DAYS_PER_YEAR
        )
        at_365 = vbt_compute_metrics(equity, timestamps, trades, periods_per_year=365)

        assert asdict(explicit_const) == asdict(default)
        assert at_365.annual_return != default.annual_return
        # Compounded annualisation formula follows periods_per_year exactly.
        # N+1 equity points yield N return periods, so the exponent divides by
        # len(equity) - 1, matching compute_metrics' own n_periods.
        expected_annual = (1 + default.total_return) ** (365 / (len(equity) - 1)) - 1
        assert at_365.annual_return == pytest.approx(expected_annual, rel=1e-12)
        # With rf=0 the Sharpe scales by sqrt(periods_per_year).
        assert at_365.sharpe_ratio == pytest.approx(
            default.sharpe_ratio * math.sqrt(365 / 252), rel=1e-12
        )

    def test_paper_portfolio_sharpe_moves_under_trading_day_cadence(self):
        db = _memory_db()
        portfolio = _seed_paper_portfolio(db)
        _seed_paper_snapshots(db, portfolio.id)

        calendar_default = pp_compute_metrics(db, portfolio.id)
        explicit_calendar = pp_compute_metrics(
            db, portfolio.id, periods_per_year=quant_metrics.CALENDAR_DAYS_PER_YEAR
        )
        trading = pp_compute_metrics(db, portfolio.id, periods_per_year=quant_metrics.TRADING_DAYS_PER_YEAR)

        assert explicit_calendar["sharpe"] == calendar_default["sharpe"]
        assert trading["sharpe"] != calendar_default["sharpe"]
        assert trading["samples"] == calendar_default["samples"] == 24


# ---------------------------------------------------------------------------
# Invalid input fails fast (QA scenario: failure mode)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -252])
class TestInvalidPeriodsPerYearRaisesValueError:
    def test_backtest_vbt(self, bad):
        equity, timestamps, trades = _vbt_fixture()

        with pytest.raises(ValueError, match="periods_per_year"):
            vbt_compute_metrics(equity, timestamps, trades, periods_per_year=bad)

    def test_paper_portfolio_before_any_db_work(self, bad):
        db = _memory_db()
        portfolio = _seed_paper_portfolio(db, user_id=f"user-{bad}")
        snapshots_before = db.query(PaperSnapshot).count()

        with pytest.raises(ValueError, match="periods_per_year"):
            pp_compute_metrics(db, portfolio.id, periods_per_year=bad)

        assert db.query(PaperSnapshot).count() == snapshots_before
