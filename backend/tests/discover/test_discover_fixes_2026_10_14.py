"""Plan 2026-10-14, WP-A6: Discover's risks (M3), currencies (M4) and fit (M5)."""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.discover import pipeline as pipeline_mod
from app.decision.discover.composite import compute_weighted_composite, derive_signals_from_scores
from app.decision.discover.dossier_writer import deterministic_risks, deterministic_template, merge_risks
from app.foundation.core.db import Base


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _rows(closes) -> list[dict]:
    start = date(2024, 1, 1)
    days = pd.bdate_range(start, periods=len(closes))
    return [{"date": d.date().isoformat(), "close": float(c)} for d, c in zip(days, closes)]


# --- M3 -------------------------------------------------------------------------

_EARNINGS = {"sentiment_fundamentals": {"next_earnings_date": "2026-10-19", "days_to_earnings": 5}}


def test_the_earnings_risk_survives_an_llm_list_that_leaves_it_out():
    earnings, rest = deterministic_risks(_EARNINGS, {"insider_cluster_buy_score": 3})
    assert earnings and earnings[0].startswith("Earnings event:")
    # The LLM repeats the prompt's insider line and drops the earnings one.
    merged = merge_risks([*earnings, *rest], ["Valuation risk: the multiple is high.", rest[0]])
    assert merged[0].startswith("Earnings event:")
    assert sum(r.lower().startswith("insider activity") for r in merged) == 1
    assert "Valuation risk: the multiple is high." in merged


def test_the_template_keeps_its_direction_when_ibes_shows_a_surprise():
    dossier = deterministic_template(
        {"symbol": "ABC"}, {"composite": 0.6}, {"estimate_sue": 2.0}, direction="hold",
    )
    assert dossier["direction"] == "hold"
    assert any("beat consensus" in r for r in dossier["key_risks"])


# --- M4 -------------------------------------------------------------------------


def test_momentum_ranks_a_dollar_stock_on_its_euro_return(monkeypatch):
    path = list(100 * np.cumprod(np.full(300, 1.001)))
    series = {"AAPL": _rows(path), "SAP.DE": _rows(path)}
    monkeypatch.setattr(pipeline_mod, "market_history", lambda _db, sym, days=0, **_kw: series[sym])
    # The dollar loses 20 % against the euro over the window.
    days = [r["date"] for r in series["AAPL"]]
    eur_per_usd = dict(zip(days, np.linspace(1.0, 0.8, len(days))))
    monkeypatch.setattr(
        "app.foundation.eur_prices.eur_per_unit_by_date",
        lambda _db, ccy, days=0, **_kw: eur_per_usd if ccy == "USD" else None,
    )
    db = _memory_db()
    usd, _ = pipeline_mod.stage_momentum_quality(db, "AAPL")
    eur, _ = pipeline_mod.stage_momentum_quality(db, "SAP.DE")
    assert usd is not None and eur is not None
    assert usd["fx_basis"] == "eur" and eur["fx_basis"] == "eur"
    assert usd["momentum_12_1m"] < eur["momentum_12_1m"] - 0.1


def test_momentum_without_a_rate_stays_local_and_says_so(monkeypatch):
    path = list(100 * np.cumprod(np.full(300, 1.001)))
    monkeypatch.setattr(pipeline_mod, "market_history", lambda _db, sym, days=0, **_kw: _rows(path))
    monkeypatch.setattr("app.foundation.eur_prices.eur_per_unit_by_date", lambda *a, **k: None)
    scores, reject = pipeline_mod.stage_momentum_quality(_memory_db(), "AAPL")
    assert reject is None and scores is not None and scores["fx_basis"] == "local"
    assert any("listing currency" in c for c in scores["concerns"])


# --- M5 -------------------------------------------------------------------------


def test_no_portfolio_drops_the_fit_and_the_composite_renormalises():
    with_fit = derive_signals_from_scores({"portfolio_fit": {"fit_score": 0.9}})
    without = derive_signals_from_scores({"portfolio_fit": {"fit_score": None}})
    assert "portfolio" in with_fit and "portfolio" not in without
    rest = {k: v for k, v in with_fit.items() if k != "portfolio"}
    assert compute_weighted_composite(without) == pytest.approx(compute_weighted_composite(rest))


def test_an_empty_book_has_no_fit(monkeypatch):
    monkeypatch.setattr(pipeline_mod, "market_history", lambda _db, sym, days=0, **_kw: _rows(np.linspace(100, 120, 400)))
    monkeypatch.setattr("app.foundation.eur_prices.to_eur", lambda _db, _s, closes, **_kw: (closes, "EUR"))
    monkeypatch.setattr(pipeline_mod, "build_real_price_matrix", lambda *a, **k: None)
    scores, reject = pipeline_mod.stage_portfolio_fit(_memory_db(), "u", "ABC")
    assert reject is None and scores is not None and scores["fit_score"] is None


def test_the_book_is_weighted_by_value(monkeypatch):
    rng = np.random.default_rng(5)
    n = 520
    big = rng.normal(0.0005, 0.01, n)
    small = rng.normal(0.0005, 0.01, n)
    idx = pd.bdate_range("2024-01-01", periods=n)
    port = pd.DataFrame({"BIG": 100 * np.cumprod(1 + big), "SMALL": 100 * np.cumprod(1 + small)}, index=idx)
    # The candidate moves with the big line only.
    cand = _rows(100 * np.cumprod(1 + big + rng.normal(0, 0.001, n)))
    monkeypatch.setattr(pipeline_mod, "market_history", lambda _db, sym, days=0, **_kw: cand)
    monkeypatch.setattr("app.foundation.eur_prices.to_eur", lambda _db, _s, closes, **_kw: (closes, "EUR"))
    monkeypatch.setattr(pipeline_mod, "build_real_price_matrix", lambda *a, **k: port)

    monkeypatch.setattr(pipeline_mod, "_book_weights", lambda *a: {"BIG": 0.9, "SMALL": 0.1})
    weighted, _ = pipeline_mod.stage_portfolio_fit(_memory_db(), "u", "ABC")
    monkeypatch.setattr(pipeline_mod, "_book_weights", lambda *a: None)
    equal, _ = pipeline_mod.stage_portfolio_fit(_memory_db(), "u", "ABC")
    assert weighted["weight_basis"] == "value" and equal["weight_basis"] == "equal"
    assert weighted["correlation"] > equal["correlation"] + 0.1
    assert weighted["fit_score"] < equal["fit_score"]


def test_book_weights_come_from_the_synced_positions(monkeypatch):
    from types import SimpleNamespace

    positions = [
        SimpleNamespace(ticker="EUNL.DE", value=9000), SimpleNamespace(ticker="SAP.DE", value=1000),
        SimpleNamespace(ticker=None, value=500),
    ]
    monkeypatch.setattr("app.foundation.live_positions.combined_positions", lambda *a, **k: positions)
    weights = pipeline_mod._book_weights(_memory_db(), "u", ["EUNL.DE", "SAP.DE"])
    assert weights == pytest.approx({"EUNL.DE": 0.9, "SAP.DE": 0.1})
