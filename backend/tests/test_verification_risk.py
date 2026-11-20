"""Tests for Portfolio -> Risk: return-based metrics, exposure and risk alerts.

VaR, drawdown and Sharpe/Sortino come from the price history of the current
holdings, never from ``PortfolioSnapshot.total_value`` (which counts deposits
and purchases as return). Only the provider boundary (``market.history``) is
stubbed; the price service, weighting and metrics run for real.
"""
from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import (
    Holding,
    Portfolio,
    PortfolioSnapshot,
    User,
)
from app.decision.verification.risk import (
    RiskAssessment,
    _drawdown_series,
    _hhi,
    _historical_var,
    _sharpe,
    _sortino,
    check_risk_alerts,
    compute_risk_metrics,
    portfolio_return_series,
    risk_assessment_with_alerts,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _make_user(db) -> User:
    user = User(id=str(uuid4()), username=f"user_{uuid4().hex[:8]}", password_hash="x")
    db.add(user)
    db.flush()
    return user


def _make_portfolio(db, user_id: str, currency: str = "EUR") -> Portfolio:
    p = Portfolio(id=str(uuid4()), user_id=user_id, name="Test", currency=currency)
    db.add(p)
    db.flush()
    return p


def _make_holding(
    db,
    portfolio_id: str,
    name: str = "Acme Corp",
    asset_type: str = "stock",
    qty: float = 100.0,
    price: float = 50.0,
    currency: str = "EUR",
    ticker: str | None = None,
) -> Holding:
    h = Holding(
        id=str(uuid4()),
        portfolio_id=portfolio_id,
        name=name,
        ticker=ticker,
        asset_type=asset_type,
        quantity=Decimal(str(qty)),
        avg_buy_price=Decimal(str(price)),
        currency=currency,
    )
    db.add(h)
    db.flush()
    return h


def _series(n: int, daily: list[float], start: float = 100.0) -> list[float]:
    """*n* closes following the repeating daily-return pattern *daily*."""
    closes = [start]
    for i in range(n - 1):
        closes.append(closes[-1] * (1.0 + daily[i % len(daily)]))
    return closes


@pytest.fixture
def prices(monkeypatch):
    """Provider stub: ``prices({"AAA": closes, ...})`` serves business-day bars ending today."""
    store: dict[str, list[float]] = {}

    def fake_history(db, ticker, days=730, **_kwargs):
        closes = store.get(ticker.upper(), [])
        out = []
        day = date.today()
        for close in reversed(closes):
            while day.weekday() >= 5:
                day -= timedelta(days=1)
            out.append({"date": day, "close": close})
            day -= timedelta(days=1)
        return list(reversed(out))

    monkeypatch.setattr("app.foundation.portfolio_price_service.market_service.history", fake_history)
    # The stub tickers have no listing suffix (read as USD); FX is tested in test_eur_prices.
    monkeypatch.setattr(
        "app.foundation.portfolio_price_service.to_eur", lambda db, symbol, closes, **kw: (dict(closes), "EUR"),
    )

    def register(mapping: dict[str, list[float]]) -> None:
        store.update({k.upper(): v for k, v in mapping.items()})

    return register


def _expected_returns(closes: list[float]) -> list[float]:
    return [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------

class TestHistoricalVar:
    def test_basic(self):
        returns = [0.01, -0.02, 0.005, -0.03, 0.02, -0.01, 0.015, -0.025, 0.008, -0.015]
        var_95 = _historical_var(returns, 0.95)
        assert var_95 >= 0
        assert isinstance(var_95, float)

    def test_empty_returns(self):
        assert _historical_var([], 0.95) == 0.0

    def test_single_positive_return_has_no_loss(self):
        # A lone +5% observation is a gain, not a loss: VaR must be 0, not 0.05.
        assert _historical_var([0.05], 0.95) == 0.0

    def test_single_negative_return_is_the_loss(self):
        # Documented convention change (ADR 0003 decision 1): _historical_var
        # delegates to the canonical quant_metrics.historical_var, which
        # returns 0.0 for series shorter than two observations.
        assert _historical_var([-0.05], 0.95) == 0.0


class TestSharpe:
    def test_positive_returns(self):
        returns = [0.01, 0.02, -0.005, 0.015, 0.008, -0.003, 0.012, 0.018, -0.007, 0.011] * 3
        assert _sharpe(returns) > 0

    def test_insufficient_data(self):
        assert _sharpe([0.01]) == 0.0
        assert _sharpe([]) == 0.0

    def test_zero_stdev(self):
        assert _sharpe([0.0, 0.0, 0.0]) == 0.0


class TestSortino:
    def test_positive_returns(self):
        returns = [0.01, 0.005, -0.01, 0.015, -0.005, 0.008, -0.012, 0.02, -0.003, 0.012] * 3
        assert _sortino(returns) > 0

    def test_all_negative(self):
        returns = [-0.01, -0.02, -0.005, -0.015, -0.01] * 7  # 35 samples
        assert _sortino(returns) < 0

    def test_insufficient_data(self):
        assert _sortino([0.01]) == 0.0


class TestDrawdownSeries:
    def test_monotonic_up(self):
        assert _drawdown_series([0.01, 0.01, 0.01, 0.01]) == (0.0, 0.0)

    def test_with_drawdown(self):
        max_dd, current_dd = _drawdown_series([0.05, 0.05, -0.10, 0.03])
        assert max_dd < 0
        assert current_dd == pytest.approx(1.05 * 1.05 * 0.9 * 1.03 / (1.05 * 1.05) - 1)

    def test_empty(self):
        assert _drawdown_series([]) == (0.0, 0.0)


class TestHhi:
    def test_single_position(self):
        assert _hhi([1.0]) == 1.0

    def test_equal_positions(self):
        assert _hhi([0.5, 0.5]) == pytest.approx(0.5)

    def test_empty(self):
        assert _hhi([]) == 0.0

    def test_zero_weights(self):
        assert _hhi([0.0, 0.0]) == 0.0

    def test_three_equal(self):
        assert _hhi([1 / 3, 1 / 3, 1 / 3]) == pytest.approx(1 / 3, abs=0.01)


# ---------------------------------------------------------------------------
# portfolio_return_series
# ---------------------------------------------------------------------------

class TestPortfolioReturnSeries:
    def test_single_holding_returns_are_the_price_returns(self, prices):
        db = _memory_db()()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        _make_holding(db, portfolio.id, ticker="AAA", qty=10, price=100)
        closes = _series(41, [0.01, -0.005, 0.002])
        prices({"AAA": closes})

        returns, basis = portfolio_return_series(db, user.id)

        assert returns == pytest.approx(_expected_returns(closes))
        assert basis.n_obs == 40
        assert basis.priced_assets == ["AAA"]
        assert basis.start and basis.end and basis.start < basis.end

    def test_two_holdings_are_value_weighted(self, prices):
        db = _memory_db()()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        _make_holding(db, portfolio.id, name="A", ticker="AAA", qty=30, price=100)  # 3000
        _make_holding(db, portfolio.id, name="B", ticker="BBB", qty=10, price=100)  # 1000
        a = _series(12, [0.02])
        b = _series(12, [-0.01])
        prices({"AAA": a, "BBB": b})

        returns, _basis = portfolio_return_series(db, user.id)

        # Weighted at market value (last close), not at cost.
        wa = 30 * a[-1] / (30 * a[-1] + 10 * b[-1])
        expected = [wa * ra + (1 - wa) * rb for ra, rb in zip(_expected_returns(a), _expected_returns(b))]
        assert returns == pytest.approx(expected)

    def test_no_priced_holdings_gives_empty_series_and_a_message(self, prices):
        db = _memory_db()()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        _make_holding(db, portfolio.id, ticker="NOPRICES", qty=1, price=10)

        returns, basis = portfolio_return_series(db, user.id)

        assert returns == []
        assert basis.n_obs == 0
        assert basis.missing_history == ["NOPRICES"]

    def test_window_is_capped_to_about_a_year(self, prices):
        db = _memory_db()()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        _make_holding(db, portfolio.id, ticker="AAA", qty=1, price=100)
        prices({"AAA": _series(400, [0.001, -0.0005])})

        returns, basis = portfolio_return_series(db, user.id)

        assert len(returns) == 260 == basis.n_obs


# ---------------------------------------------------------------------------
# compute_risk_metrics
# ---------------------------------------------------------------------------

class TestComputeRiskMetrics:
    def test_portfolio_not_found(self):
        db = _memory_db()()
        result = compute_risk_metrics("nonexistent", db)
        assert isinstance(result, RiskAssessment)
        assert result.var_95 == 0.0

    def test_no_holdings(self, prices):
        db = _memory_db()()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        result = compute_risk_metrics(portfolio.id, db)
        assert result.var_95 == 0.0
        assert result.sharpe_ratio == 0.0
        assert result.insufficient_history is True
        assert result.return_basis.n_obs == 0

    def test_var_and_drawdown_match_the_price_series(self, prices):
        db = _memory_db()()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        _make_holding(db, portfolio.id, name="Stock A", ticker="AAA", asset_type="stock", qty=100, price=50)
        _make_holding(db, portfolio.id, name="ETF B", ticker="BBB", asset_type="etf", qty=50, price=100)
        a = _series(61, [0.012, -0.02, 0.004, -0.006])
        b = _series(61, [0.006, -0.004, 0.003])
        prices({"AAA": a, "BBB": b})

        result = compute_risk_metrics(portfolio.id, db)

        wa = 100 * a[-1] / (100 * a[-1] + 50 * b[-1])  # market-value weights
        expected = [wa * ra + (1 - wa) * rb for ra, rb in zip(_expected_returns(a), _expected_returns(b))]
        assert result.var_95 == pytest.approx(_historical_var(expected, 0.95))
        assert result.var_99 == pytest.approx(_historical_var(expected, 0.99))
        max_dd, current_dd = _drawdown_series(expected)
        assert result.max_drawdown == pytest.approx(max_dd)
        assert result.current_drawdown == pytest.approx(current_dd)
        assert result.return_basis.n_obs == 60
        assert result.insufficient_history is False
        assert set(result.asset_type_exposure) == {"stock", "etf"}
        assert sum(result.asset_type_exposure.values()) == pytest.approx(1.0)
        assert len(result.currency_exposure) == 1

    def test_deposits_and_purchases_in_snapshots_do_not_move_risk(self, prices):
        """Regression: snapshot total_value = cash + securities, so a salary or a
        purchase read as a return. Risk must come from prices only."""
        db = _memory_db()()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        _make_holding(db, portfolio.id, ticker="AAA", qty=100, price=50)
        calm = _series(61, [0.002, 0.001, 0.003])  # never falls
        prices({"AAA": calm})
        today = date.today()
        # Account balance doubling, then crashing back as money moves in and out.
        for i, value in enumerate([10_000, 20_000, 5_000, 40_000, 8_000, 30_000] * 6):
            db.add(PortfolioSnapshot(
                id=str(uuid4()), user_id=user.id, portfolio_id=portfolio.id,
                date=today - timedelta(days=35 - i), total_value=Decimal(value),
                cash_value=Decimal(0), security_value=Decimal(value),
            ))
        db.flush()

        result = compute_risk_metrics(portfolio.id, db)

        assert result.max_drawdown == 0.0
        assert result.current_drawdown == 0.0
        assert result.var_95 == 0.0
        assert result.var_99 == 0.0

    def test_concentration_index(self, prices):
        db = _memory_db()()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        _make_holding(db, portfolio.id, name="All-in", qty=1, price=10000)
        result = compute_risk_metrics(portfolio.id, db)
        assert result.concentration_index == pytest.approx(1.0)

    def test_assessment_serialises_with_the_return_basis(self, prices):
        d = asdict(RiskAssessment(insufficient_history=True))
        assert d["insufficient_history"] is True
        assert d["return_basis"]["source"] == "holdings_price_history"
        assert "asset_type_exposure" in d and "sector_exposure" not in d


class TestInsufficientHistory:
    """Sharpe and Sortino are 0.0 (and insufficient_history True) below 30 daily returns."""

    def _book(self, db, prices, n_prices: int, pattern: list[float]):
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        _make_holding(db, portfolio.id, ticker="AAA", qty=100, price=50)
        prices({"AAA": _series(n_prices, pattern)})
        return portfolio

    def test_flag_true_below_threshold(self, prices):
        db = _memory_db()()
        portfolio = self._book(db, prices, 10, [-0.005])
        assert compute_risk_metrics(portfolio.id, db).insufficient_history is True

    def test_flag_false_at_threshold(self, prices):
        db = _memory_db()()
        portfolio = self._book(db, prices, 31, [0.002, -0.001])  # 30 returns
        assert compute_risk_metrics(portfolio.id, db).insufficient_history is False

    def test_ratios_suppressed_but_var_and_drawdown_still_reported(self, prices):
        db = _memory_db()()
        portfolio = self._book(db, prices, 6, [-0.10, -0.12, -0.2, -0.1, -0.3])
        result = compute_risk_metrics(portfolio.id, db)
        assert result.insufficient_history is True
        assert result.sharpe_ratio == 0.0
        assert result.sortino_ratio == 0.0
        assert result.var_95 > 0.0
        assert result.max_drawdown < 0.0


# ---------------------------------------------------------------------------
# check_risk_alerts / risk_assessment_with_alerts
# ---------------------------------------------------------------------------

class TestCheckRiskAlerts:
    def test_drawdown_warning_and_critical_follow_the_current_drawdown(self, prices):
        db = _memory_db()()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        _make_holding(db, portfolio.id, name="ETF", ticker="AAA", asset_type="etf", qty=100, price=75)

        # -12 %: warning at the default -10 % threshold.
        prices({"AAA": [100, 101, 102, 103, 104, 105, 100, 95, 92.4]})
        warn = [a for a in check_risk_alerts(portfolio.id, db) if a.type == "drawdown"]
        assert [a.severity for a in warn] == ["warning"]
        assert "below their peak" in warn[0].message

        # -30 %: critical at the default -20 % threshold.
        prices({"AAA": [100, 105, 110, 100, 90, 80, 77]})
        crit = [a for a in check_risk_alerts(portfolio.id, db) if a.type == "drawdown"]
        assert [a.severity for a in crit] == ["critical"]

    def test_no_drawdown_alert_when_the_book_is_near_its_peak(self, prices):
        db = _memory_db()()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        _make_holding(db, portfolio.id, ticker="AAA", asset_type="etf", qty=100, price=75)
        prices({"AAA": _series(40, [0.003, -0.001])})
        assert [a for a in check_risk_alerts(portfolio.id, db) if a.type == "drawdown"] == []

    def test_holding_level_hhi_is_no_longer_an_alert(self, prices):
        db = _memory_db()()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        _make_holding(db, portfolio.id, name="All-in Stock", qty=1, price=10000)
        alerts = check_risk_alerts(portfolio.id, db)
        assert [a for a in alerts if a.severity in ("warning", "critical")] == []

    def test_no_sector_or_sharpe_alerts(self, prices):
        db = _memory_db()()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        _make_holding(db, portfolio.id, name="Stock A", ticker="AAA", asset_type="stock", qty=140, price=50)
        _make_holding(db, portfolio.id, name="ETF B", ticker="BBB", asset_type="etf", qty=30, price=100)
        prices({"AAA": _series(45, [-0.01]), "BBB": _series(45, [-0.004])})  # losing book
        types = {a.type for a in check_risk_alerts(portfolio.id, db)}
        assert not types & {"sector", "correlation"}

    def test_currency_alert_for_a_foreign_priced_majority(self, prices):
        db = _memory_db()()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id, currency="EUR")
        _make_holding(db, portfolio.id, name="US Stock", currency="USD", qty=140, price=50)
        _make_holding(db, portfolio.id, name="EU ETF", currency="EUR", qty=30, price=100)
        alerts = check_risk_alerts(portfolio.id, db)
        assert [a for a in alerts if a.type == "currency"]

    def test_alerts_reuse_a_given_assessment(self, prices, monkeypatch):
        db = _memory_db()()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        _make_holding(db, portfolio.id, qty=1, price=100)
        assessment = RiskAssessment(current_drawdown=-0.25)
        assessment.return_basis.n_obs = 200

        def boom(*_a, **_k):
            raise AssertionError("assessment must not be recomputed")

        monkeypatch.setattr("app.decision.verification.risk.compute_risk_metrics", boom)
        alerts = check_risk_alerts(portfolio.id, db, assessment)
        assert [a.severity for a in alerts if a.type == "drawdown"] == ["critical"]


class TestRiskAssessmentWithAlerts:
    def test_merges_single_name_concentration_alerts_critical_first(self, prices):
        db = _memory_db()()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        _make_holding(db, portfolio.id, name="Mega Stock", qty=80, price=100)
        _make_holding(db, portfolio.id, name="Other", qty=20, price=100)

        assessment, alerts = risk_assessment_with_alerts(portfolio.id, db)

        assert [a.type for a in alerts] == ["concentration_single"]
        assert alerts[0].severity == "critical"
        assert assessment.alerts == alerts

    def test_healthy_etf_portfolio_has_no_actionable_alert(self, prices):
        db = _memory_db()()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        # .DE listings are euro-priced, so no foreign-currency alert either.
        _make_holding(db, portfolio.id, name="World ETF", ticker="AAA.DE", asset_type="etf", qty=60, price=100)
        _make_holding(db, portfolio.id, name="S&P ETF", ticker="BBB.DE", asset_type="etf", qty=30, price=100)
        _make_holding(db, portfolio.id, name="Bond D", ticker="CCC.DE", asset_type="bond", qty=10, price=100)
        prices({t: _series(40, [0.002, -0.0008]) for t in ("AAA.DE", "BBB.DE", "CCC.DE")})

        _assessment, alerts = risk_assessment_with_alerts(portfolio.id, db)

        assert [a for a in alerts if a.severity in ("warning", "critical")] == []
