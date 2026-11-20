"""Regression test for the Factor Dashboard weight bug.

factors_dashboard() looked up weights.get(name, 0) where `name` is a factor
label ("market"/"size"/...) but `weights` is keyed by portfolio-holding
ticker — always a miss. See docs/archive/audits/2026-08-26/deepdive-03-quantlab.md
Issue 4b.
"""
import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.interface.api.quant.factors import factors_dashboard
from app.foundation.portfolio_price_service import PortfolioPriceService


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


class _FakeUser:
    id = "user-1"


def test_factor_dashboard_weight_uses_proxy_ticker_not_factor_name(monkeypatch):
    """weights is ticker-keyed; the 'weight' field must resolve via the
    factor's proxy ticker (proxies[name]), not the bare factor label."""
    db = _memory_db()

    matrix = {
        "EUNL.DE": {"2026-01-01": 100.0, "2026-01-02": 101.0},
        "AAPL": {"2026-01-01": 50.0, "2026-01-02": 51.0},
    }
    # weights is ticker-keyed (portfolio holding value), never a factor label.
    weights = {"EUNL.DE": 3000.0, "AAPL": 1000.0}
    proxies = {"market": "EUNL.DE", "size": "AAPL"}

    portfolio_returns = pd.Series([0.01] * 40)
    factor_frame = pd.DataFrame({"market": [0.01] * 40, "size": [0.02] * 40})

    monkeypatch.setattr(
        PortfolioPriceService, "price_matrix", lambda self: (matrix, weights, {})
    )
    monkeypatch.setattr(
        PortfolioPriceService, "weighted_returns", lambda self, m, w: portfolio_returns
    )
    monkeypatch.setattr(
        PortfolioPriceService, "factor_proxy_returns", lambda self: factor_frame
    )
    monkeypatch.setattr(PortfolioPriceService, "_get_factor_proxies", lambda self: proxies)
    monkeypatch.setattr(
        PortfolioPriceService,
        "to_frame",
        lambda self, m: pd.DataFrame({"EUNL.DE": [100.0, 101.0], "AAPL": [50.0, 51.0]}),
    )

    result = factors_dashboard(db=db, user=_FakeUser())

    by_name = {row["name"]: row for row in result["factor_exposures"]}
    total = weights["EUNL.DE"] + weights["AAPL"]
    assert by_name["market"]["weight"] == round(weights["EUNL.DE"] / total, 4)
    assert by_name["size"]["weight"] == round(weights["AAPL"] / total, 4)
    # The bug's symptom: both would have been 0.0 (factor-name keys never
    # exist in a ticker-keyed dict).
    assert by_name["market"]["weight"] != 0.0
    assert by_name["size"]["weight"] != 0.0
