"""EUR restatement of closes and the date-aligned benchmark statistics."""
import math
from unittest.mock import patch

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation import eur_prices, quant_metrics
from app.foundation.core.db import Base


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_eur_listing_is_unchanged():
    closes = {"2026-01-02": 100.0, "2026-01-05": 101.0}
    out, ccy = eur_prices.to_eur(_memory_db(), "EUNL.DE", closes)
    assert out == closes and ccy == "EUR"


def test_usd_listing_carries_the_currency_return():
    """R_EUR = (1 + R_USD)(1 + R_FX) - 1."""
    usd_per_eur = {"2026-01-02": 1.10, "2026-01-05": 1.00}  # the dollar strengthens 10 %
    with patch.object(eur_prices.market_service, "usd_per_unit_by_date", return_value=usd_per_eur):
        out, ccy = eur_prices.to_eur(_memory_db(), "AMC", {"2026-01-02": 100.0, "2026-01-05": 105.0})
    assert ccy == "USD"
    r_eur = out["2026-01-05"] / out["2026-01-02"] - 1
    assert r_eur == pytest.approx((1.05) * (1.10 / 1.00) - 1)


def test_pence_are_divided_before_conversion():
    usd = {"2026-01-02": 1.25}  # USD per GBP and USD per EUR, used for both
    with (
        patch.object(eur_prices, "resolve_quote_currency", return_value="GBp"),
        patch.object(eur_prices.market_service, "usd_per_unit_by_date", side_effect=lambda db, c, **kw: {
            "EUR": {"2026-01-02": 1.10}, "GBP": usd,
        }[c]),
    ):
        out, ccy = eur_prices.to_eur(_memory_db(), "SHEL.L", {"2026-01-02": 2500.0})
    assert ccy == "GBP"
    assert out["2026-01-02"] == pytest.approx(25.0 * 1.25 / 1.10)


def test_a_close_without_a_recent_rate_is_dropped():
    usd_per_eur = {"2026-01-02": 1.10}
    with patch.object(eur_prices.market_service, "usd_per_unit_by_date", return_value=usd_per_eur):
        out, _ = eur_prices.to_eur(_memory_db(), "AMC", {"2026-01-05": 1.0, "2026-01-20": 1.0})
    assert list(out) == ["2026-01-05"]  # 3 days carried; 18 days is too stale


def test_no_rate_means_no_series_rather_than_unconverted_dollars():
    with patch.object(eur_prices.market_service, "usd_per_unit_by_date", return_value=None):
        out, ccy = eur_prices.to_eur(_memory_db(), "AMC", {"2026-01-05": 1.0})
    assert out == {} and ccy == "USD"


def test_benchmark_defaults_to_msci_world_eur():
    assert eur_prices.benchmark_ticker(_memory_db()) == "EUNL.DE"


# --- date alignment and the regression ----------------------------------------


def test_align_by_date_pairs_the_same_days_only():
    dates, left, right = quant_metrics.align_by_date(
        {"d1": 0.01, "d2": 0.02, "d4": 0.04}, {"d2": 0.2, "d3": 0.3, "d4": 0.4},
    )
    assert dates == ["d2", "d4"] and left == [0.02, 0.04] and right == [0.2, 0.4]


def test_a_shifted_benchmark_no_longer_reads_as_uncorrelated():
    """The 2026-10-04 bug: a book that is 99.9 % EUNL read beta -0.05, R^2 0.002
    because the two series were paired by position over different windows."""
    rng = np.random.default_rng(0)
    days = [f"2025-{m:02d}-{d:02d}" for m in range(1, 13) for d in range(1, 29)]
    bench = dict(zip(days, rng.normal(0, 0.01, len(days))))
    port = {d: bench[d] for d in days[:-30]}  # the book's history ends earlier
    br_long = list(bench.values())
    by_position = quant_metrics.r_squared(list(port.values()), br_long)
    _, p, b = quant_metrics.align_by_date(port, bench)
    assert by_position < 0.1
    assert quant_metrics.r_squared(p, b) == pytest.approx(1.0)
    assert quant_metrics.beta(p, b) == pytest.approx(1.0)


def test_alpha_is_the_annualised_regression_intercept():
    rng = np.random.default_rng(1)
    b = rng.normal(0.0004, 0.01, 1000)
    p = 0.0002 + 0.8 * b + rng.normal(0, 0.002, 1000)
    rf = 0.02
    reg = quant_metrics.benchmark_regression(p, b, risk_free=rf)
    X = np.column_stack([np.ones_like(b), b - rf / 252])
    coef, *_ = np.linalg.lstsq(X, p - rf / 252, rcond=None)
    assert reg["alpha"] == pytest.approx(coef[0] * 252)
    assert reg["beta"] == pytest.approx(coef[1])
    assert quant_metrics.alpha(p, b, rf) == pytest.approx(reg["alpha"])
    assert reg["alpha_t_stat"] > 2  # a true 5 % a year with tiny noise is significant
    assert reg["n"] == 1000


def test_alpha_t_stat_matches_ols_without_autocorrelation_lags():
    rng = np.random.default_rng(2)
    b = rng.normal(0, 0.01, 500)
    p = 0.5 * b + rng.normal(0, 0.005, 500)
    reg = quant_metrics.benchmark_regression(p, b, hac_lags=0)
    # White SE with no lags; compare against a direct sandwich computation.
    X = np.column_stack([np.ones_like(b), b])
    coef, *_ = np.linalg.lstsq(X, p, rcond=None)
    e = p - X @ coef
    bread = np.linalg.inv(X.T @ X)
    cov = bread @ (X.T * e**2) @ X @ bread * 500 / 498
    assert reg["alpha_t_stat"] == pytest.approx(coef[0] / math.sqrt(cov[0, 0]))


def test_dimson_beta_recovers_a_lagged_reaction():
    """A market that closes earlier reacts to half of the benchmark's day late."""
    rng = np.random.default_rng(3)
    b = rng.normal(0, 0.01, 2000)
    p = np.empty_like(b)
    p[0] = 0.5 * b[0]
    p[1:] = 0.5 * b[1:] + 0.5 * b[:-1]
    reg = quant_metrics.benchmark_regression(p, b)
    assert reg["beta"] == pytest.approx(0.5, abs=0.05)
    assert reg["beta_dimson"] == pytest.approx(1.0, abs=0.05)


def test_risk_series_joins_a_dated_benchmark():
    out = quant_metrics.risk_series(
        [0.01, 0.02, 0.03], ["d1", "d2", "d3"], benchmark_returns={"d2": 0.2, "d3": 0.3, "d9": 9.0},
    )
    assert out["regression_points"] == [[0.2, 0.02], [0.3, 0.03]]
