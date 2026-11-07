"""Regressions from the audit of the 2026-11-05 Discover run (each case is what that run showed)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.discover import pipeline
from app.decision.discover.candidate_gate import evaluate_track_record
from app.decision.discover.data_quality import data_quality_score, stamp_data_quality
from app.decision.discover.dossier_writer import _earnings_text, _reword_concerns_for_prompt, merge_risks, skill_horizon
from app.decision.discover.pipeline import (
    _apply_recency_penalty,
    _book_ratios_unreliable,
    _fx_pairs_for,
)
from app.decision.discover.thesis_checks import check_thesis, is_data_gap_risk, mislabel_problems
from app.foundation.broker_instruments import isin_for_us_ticker
from app.foundation.core.db import Base
from app.foundation.data_backbone.bar_guard import spike_reversal_positions
from app.foundation.models.entities import BrokerInstrument, DiscoveryPrediction


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _prediction(db, *, symbol="MPC", days_ago=30, raw=None, portfolio_id=None, excess=None):
    predicted = datetime.now(UTC) - timedelta(days=days_ago)
    db.add(DiscoveryPrediction(
        user_id="u1", run_id="r", symbol=symbol, predicted_at=predicted, horizon_days=21,
        resolve_at=predicted + timedelta(days=30), direction="buy", portfolio_id=portfolio_id,
        features_json={"signal_breakdown": {"composite_raw": raw} if raw is not None else {"suggested_weight": 0.1}},
        outcome_status="resolved" if excess is not None else "pending", excess_return=excess,
    ))
    db.commit()


# --- ledger writers ---------------------------------------------------------

def test_recency_penalty_ignores_the_advisors_rows():
    """MPC: Discover 0.594 on 10-02, advisor call on 10-05, 0.6496 now: exempt, not penalised."""
    db = _memory_db()
    _prediction(db, days_ago=34, raw=0.594)
    _prediction(db, days_ago=31, portfolio_id="paper-1")

    adjusted, detail = _apply_recency_penalty(db, "u1", "MPC", "equity", 0.6496)

    assert adjusted == 0.6496
    assert detail["exempt_reason"] == "improved_since_last"
    assert detail["last_composite"] == 0.594
    assert detail["days_since_last"] == pytest.approx(34, abs=0.1)


def test_a_name_only_the_advisor_called_was_never_recommended_by_discover():
    db = _memory_db()
    _prediction(db, days_ago=2, portfolio_id="paper-1")
    adjusted, detail = _apply_recency_penalty(db, "u1", "MPC", "equity", 0.6)
    assert adjusted == 0.6
    assert detail["exempt_reason"] == "never_recommended"


def test_track_record_of_discover_leaves_out_the_advisors_rows():
    db = _memory_db()
    for days in (200, 170, 140):
        _prediction(db, days_ago=days, portfolio_id="paper-1", excess=0.05)

    assert evaluate_track_record(db, "equity")["n"] == 0


# --- FX history -------------------------------------------------------------

def test_every_listing_currency_gets_its_pair_warmed():
    pairs = _fx_pairs_for(["7203.T", "NESN.SW", "AAPL", "SAP.DE", "SHEL.L"])
    assert pairs == ["USDCHF=X", "USDEUR=X", "USDGBP=X", "USDJPY=X"]


def test_short_fx_history_does_not_reject_a_candidate(monkeypatch):
    """7203.T had 263 bars and a 34-day USDJPY: "candidate only 28 rows"."""
    db = _memory_db()
    days = pd.bdate_range(end=datetime.now(UTC).date(), periods=300)
    rows = [{"date": d.date(), "close": 100.0 + i % 7} for i, d in enumerate(days)]
    monkeypatch.setattr(pipeline, "market_history", lambda *a, **k: rows)
    monkeypatch.setattr(pipeline, "build_real_price_matrix", lambda *a, **k: None)
    short = {str(d.date()): 0.006 for d in days[-28:]}
    monkeypatch.setattr(
        "app.foundation.eur_prices.to_eur", lambda *a, **k: (short, "JPY"),
    )

    frame = pd.DataFrame(rows)
    assert len(pipeline._closes_in_eur(db, "7203.T", frame)[0]) == 300
    out, reason = pipeline.stage_portfolio_fit(db, "u1", "7203.T")
    assert reason is None
    assert out["note"] == "no portfolio"


# --- bad prints -------------------------------------------------------------

def test_xdeq_print_is_dropped_for_a_fund_but_a_real_move_stays():
    closes = [68.0, 68.21, 79.54, 68.96, 69.2]
    assert spike_reversal_positions(closes, "equity_etf") == {2}
    assert spike_reversal_positions(closes, "single_stock") == set()
    # A crash that stays is no print; nor is a gap the next bar does not take back.
    assert spike_reversal_positions([100.0, 85.0, 84.0, 83.0], "equity_etf") == set()


def test_risk_stages_share_window_currency_and_clean_closes(monkeypatch):
    db = _memory_db()
    days = pd.bdate_range(end=datetime.now(UTC).date(), periods=280)
    closes = [100.0 + 0.05 * i for i in range(280)]
    closes[100] = 130.0
    rows = [{"date": d.date(), "close": c} for d, c in zip(days, closes)]
    asked: list[int] = []

    def history(_db, _symbol, days=0, **_kw):
        asked.append(days)
        return rows

    monkeypatch.setattr(pipeline, "market_history", history)
    eur = {str(d.date()): c * 0.9 for d, c in zip(days, closes)}
    monkeypatch.setattr("app.foundation.eur_prices.to_eur", lambda _db, _s, c, **_k: (
        {d: eur[d] for d in c if d in eur}, "USD",
    ))

    mom, _ = pipeline.stage_momentum_quality(db, "XDEQ.DE", is_etf=True)
    gate, _ = pipeline.stage_verification_gate(db, "XDEQ.DE", is_etf=True)

    assert asked == [400, 400]
    assert mom["bad_prints_dropped"] == 1
    assert gate["fx_basis"] == "eur"
    assert gate["max_drawdown"] == pytest.approx(abs(mom["max_drawdown"]))
    assert gate["max_drawdown"] < 0.01


# --- near-zero equity -------------------------------------------------------

@pytest.mark.parametrize(("name", "pb", "roe", "de", "sector", "bad"), [
    ("PM", 2936.4, 5.75, 18608.0, "Consumer Defensive", True),
    ("ABBV", 367.0, 0.96, 4921.8, "Healthcare", True),
    ("MO", 39.9, 1.50, 867.1, "Consumer Defensive", True),
    ("NVDA real ROE", 21.1, 1.10, 14.6, "Technology", False),
    ("GS leverage is its business", 2.1, 0.18, 1125.6, "Financial Services", False),
    ("MPC", 4.6, 0.77, 142.3, "Energy", False),
    ("no data", None, None, None, None, False),
])
def test_book_ratios(name, pb, roe, de, sector, bad):
    assert _book_ratios_unreliable(pb, roe, de, sector=sector, industry=None) is bad, name


# --- data quality -----------------------------------------------------------

def _scored(signals, lane="stock"):
    return {"lane": lane, "scores": {"composite_inputs": [{"signal": s} for s in signals]}}


def test_a_signal_no_stock_has_shows_and_lowers_the_quality():
    stock_signals = ["momentum", "risk", "benchmark", "portfolio", "fundamentals", "analyst", "estimate_revision"]
    results = [_scored(stock_signals) for _ in range(20)]
    results.append(_scored(["momentum", "risk", "benchmark", "portfolio"], "fund"))

    stamp_data_quality(results)

    stock = results[0]["scores"]["data_quality"]
    assert stock["weight_share"] == pytest.approx(0.70 / 0.95, abs=1e-3)
    assert "insider trading" in stock["run_gaps"]
    assert "insider trading" in stock["missing"]
    fund = results[-1]["scores"]["data_quality"]
    assert fund == {"weight_share": 1.0, "missing": [], "run_gaps": []}
    assert data_quality_score(1.0, stock) == 0.74
    assert data_quality_score(0.98, None) == 0.98


def test_data_quality_stays_out_of_the_prompt():
    out = _reword_concerns_for_prompt({
        "data_quality": {"weight_share": 0.79},
        "alpha_screener": {"regime_affinity": 1.0, "concerns": ["alphacrafter_no_validated_factors"]},
    })
    assert "data_quality" not in out
    assert "regime_affinity" not in out["alpha_screener"]
    assert "_" not in out["alpha_screener"]["concerns"][0]


# --- thesis checks ----------------------------------------------------------

@pytest.mark.parametrize("text", [
    "The recent 1-month momentum of -20.9% is weak.",
    "A 3-month momentum of 31.8% supports the case.",
    "It offers a 12-month gain of 40%.",
    "Drawdown capped at -17.24% historically.",
    "It moved with weekly returns of 0.03 against the book.",
    "Its beta suggests moderate correlation with the portfolio.",
    "The figure rests on market-implied estimates.",
    "It shows strong regime affinity.",
    "Market volatility exceeding the 6-month horizon is a concern.",
    "Flagged revenue_growth_unreliable by the provider.",
])
def test_what_the_run_wrote_is_rejected(text):
    assert mislabel_problems(text)


@pytest.mark.parametrize("text", [
    "The 12-1 month momentum of 31.8% ranks near the top of the candidates.",
    "Over the 6-month horizon the estimate is 4.2%.",
    "Beta of 0.8 and a correlation of 0.3 with the portfolio.",
    "A return of 12.5% over three years.",
    "A maximum drawdown of 17.24% in the window measured.",
])
def test_correct_wording_passes(text):
    assert mislabel_problems(text) == []


def test_strong_momentum_needs_a_high_rank():
    text = "The fund has strong momentum."
    assert mislabel_problems(text, momentum_rank=0.55)
    assert mislabel_problems(text, momentum_rank=0.91) == []


def test_diversifier_needs_a_low_correlation():
    assert mislabel_problems("It offers diversification.", correlation=0.55)
    assert mislabel_problems("It offers diversification.", correlation=0.2) == []
    assert mislabel_problems("It offers little diversification.", correlation=0.55) == []


def test_named_holdings_must_be_in_the_facts():
    facts = 'Holdings (top 10): [{"ticker": "AAPL"}]'
    text = "It has strong holdings in MSFT and AAPL."
    assert mislabel_problems(text, facts, is_fund=True) == ["holding 'MSFT' is not in the holdings given"]
    assert mislabel_problems(text, facts) == []


def test_a_data_gap_is_no_risk_and_is_dropped():
    assert is_data_gap_risk("ml_signal_unavailable")
    assert is_data_gap_risk("ML signal unavailable for this name.")
    assert not is_data_gap_risk("Tobacco regulation could cut volumes.")

    check = check_thesis(
        "A complete sentence about 12-1 month momentum.",
        ["insider_data_unavailable", "Tobacco regulation could cut volumes."],
        facts_text="12-1 month momentum", macro=None, direction="long",
    )
    assert check.ok
    assert check.key_risks == ["Tobacco regulation could cut volumes."]


def test_the_mislabel_reason_is_reported():
    check = check_thesis(
        "The 3-month momentum looks fine.", [], facts_text="", macro=None, direction="long",
    )
    assert check.reason == "mislabel"
    assert check.problems


# --- risks and rounding -----------------------------------------------------

def test_one_earnings_event_is_one_risk():
    required = ["Earnings event: Next earnings report: 2026-11-19, 14 days from now."]
    out = merge_risks(required, ["Upcoming earnings on 2026-11-19 may move the price.", "Margins may fall."])
    assert out == [required[0], "Margins may fall."]
    # Falling earnings are a different risk from the report date.
    assert merge_risks(required, ["Falling earnings hurt the valuation."]) == [
        required[0], "Falling earnings hurt the valuation.",
    ]


def test_skill_horizon_has_no_false_precision():
    text = skill_horizon(0.1282, "EUNL.DE")["text"]
    assert "657" not in text
    assert "a 1 %/yr edge more than 100 years" in text
    assert "needs about 25 years" in skill_horizon(0.05, None)["text"]
    assert "needs about 2.6 years" in skill_horizon(0.016, None)["text"]


def test_earnings_on_the_run_day_says_what_it_means():
    text = _earnings_text({"next_earnings_date": "2026-11-05", "days_to_earnings": 0})
    assert "may predate the results" in text
    assert "predate" not in _earnings_text({"next_earnings_date": "2026-11-19", "days_to_earnings": 14})


# --- ISIN -------------------------------------------------------------------

def test_a_us_tickers_isin_comes_from_trading_212s_line():
    db = _memory_db()
    db.add(BrokerInstrument(source="trading212", ticker="PM_US_EQ", isin="US7181721090", synced_at=datetime.now(UTC)))
    db.commit()
    assert isin_for_us_ticker(db, "PM") == "US7181721090"
    assert isin_for_us_ticker(db, "XYZ") is None
