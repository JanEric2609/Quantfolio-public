"""What the review of Discover run 4f3ddca7 (2026-11-08) found in the dossiers."""
from __future__ import annotations

import json

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.discover import pipeline
from app.decision.discover.dossier_writer import _grounding_prompt_lines, _reword_concerns_for_prompt, deterministic_risks
from app.foundation.core.db import Base
from app.decision.discover.thesis_checks import check_thesis, mislabel_problems


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _check(thesis, risks=(), **kwargs):
    return check_thesis(thesis, list(risks), facts_text="", macro=None, direction="long", **kwargs)


# --- "high correlation" at a moderate one ------------------------------------------


def test_high_correlation_with_the_portfolio_at_0_43_loses_its_sentence_and_risk_line():
    thesis = (
        "United Rentals shows positive momentum with a buy consensus over the window measured. "
        "The stock shows high weekly correlation to the portfolio, indicating significant overlap."
    )
    check = _check(
        thesis,
        ["Weekly correlation suggests substantial overlap with current portfolio holdings", "Cyclical demand"],
        correlation=0.43,
    )
    assert check.ok
    assert check.thesis == "United Rentals shows positive momentum with a buy consensus over the window measured."
    assert check.key_risks == ["Cyclical demand"]
    assert mislabel_problems("High correlation with the portfolio.", correlation=0.43)


def test_high_correlation_stays_where_it_is_true_or_not_about_the_portfolio():
    thesis = "The fund has a high correlation with the portfolio and adds little diversification to it."
    assert _check(thesis, correlation=0.86).thesis == thesis
    other = "Earnings carry a high correlation with oil prices over the cycle measured."
    assert _check(other, correlation=0.29).thesis == other


# --- one market beta in the prompt -------------------------------------------------


def test_the_prompt_scores_carry_only_the_weekly_beta_when_there_is_one():
    scores = {"quant_signals": {"market_beta": 1.1241, "market_beta_weekly": 0.8824}}
    text = json.dumps(_reword_concerns_for_prompt(scores))
    assert "0.8824" in text and "1.1241" not in text


def test_the_daily_beta_stays_when_no_weekly_one_exists():
    scores = {"quant_signals": {"market_beta": 1.1241, "market_beta_weekly": None}}
    assert "1.1241" in json.dumps(_reword_concerns_for_prompt(scores))


# --- momentum from one jump, and a volatility past any reason -----------------------

def _stub_history(monkeypatch, closes):
    rows = [
        {"date": d.date().isoformat(), "close": c}
        for d, c in zip(pd.bdate_range("2025-06-02", periods=len(closes)), closes, strict=True)
    ]
    monkeypatch.setattr(pipeline, "market_history", lambda db, symbol, days: rows)
    monkeypatch.setattr(pipeline, "_without_spike_reversals", lambda db, symbol, df, is_etf=False: (df, 0))
    monkeypatch.setattr(pipeline, "_closes_in_eur", lambda db, symbol, df: (df, "eur"))
    return rows


def test_a_jump_is_measured_and_the_stock_ranks_without_it(monkeypatch):
    # A steady 1 % climb a week, then one +177 % day, then flat: MRNA-like.
    closes = [25.0 * (1.002 ** i) for i in range(150)]
    closes += [closes[-1] * 2.77] + [closes[-1] * 2.77] * 109
    rows = _stub_history(monkeypatch, closes)
    mom, error = pipeline.stage_momentum_quality(_memory_db(), "MRNA")
    assert error is None and mom is not None
    assert mom["largest_day_return"] == 1.77
    assert mom["largest_day_date"] == rows[150]["date"]
    assert 0.33 < mom["largest_day_share"] < 1.0
    assert mom["jump_driven"] is True
    assert mom["momentum_without_largest_day"] < mom["momentum_12_1m"]
    assert pipeline._momentum_for_rank(mom) == mom["momentum_without_largest_day"]


def test_a_steady_climb_ranks_on_its_whole_return(monkeypatch):
    _stub_history(monkeypatch, [25.0 * (1.003 ** i) for i in range(260)])
    mom, _ = pipeline.stage_momentum_quality(_memory_db(), "STEADY")
    assert mom is not None
    assert mom["largest_day_share"] < 0.05
    assert mom["jump_driven"] is False
    assert mom["information_discreteness"] == -1.0
    assert pipeline._momentum_for_rank(mom) == mom["momentum_12_1m"]


def test_older_stored_scores_rank_as_before():
    assert pipeline._momentum_for_rank({"momentum_12_1m": 8.48}) == 8.48


def test_the_jump_reaches_the_prompt_and_the_risks():
    mom = {"largest_day_share": 0.4533, "largest_day_date": "2026-08-19", "largest_day_return": 1.77, "jump_driven": True}
    _, rest = deterministic_risks({"momentum_quality": mom}, {})
    assert rest == ["One-day jump: 2026-08-19 (+177.0%) gave 45% of the 12-1 month return."]
    assert "2026-08-19 (+177.0%)" in _grounding_prompt_lines({"momentum_quality": mom}, {})
    assert deterministic_risks({"momentum_quality": {**mom, "jump_driven": False}}, {}) == ([], [])


def test_volatility_past_100_percent_is_rejected(monkeypatch):
    closes = [100.0 * (1.25 if i % 2 else 0.8) ** (i % 2) for i in range(260)]
    _stub_history(monkeypatch, closes)
    scores, error = pipeline.stage_verification_gate(_memory_db(), "LOTTO")
    assert scores is None
    assert error is not None and "exceeds 100%" in error


def test_volatility_of_88_percent_is_flagged_not_rejected(monkeypatch):
    closes = [100.0 * (1.055 if i % 2 else 1.0) for i in range(260)]
    _stub_history(monkeypatch, closes)
    scores, error = pipeline.stage_verification_gate(_memory_db(), "MRVL")
    assert error is None and scores is not None
    assert 0.6 < scores["volatility"] < 1.0


def test_a_modest_year_with_one_good_day_is_no_jump(monkeypatch):
    # +15 % in a year with one +6 % day: the day is a third of the year, but no event.
    closes = [100.0 * (1.0003 ** i) for i in range(200)]
    closes += [closes[-1] * 1.06 * (1.0003 ** i) for i in range(60)]
    _stub_history(monkeypatch, closes)
    mom, _ = pipeline.stage_momentum_quality(_memory_db(), "MODEST")
    assert mom is not None
    assert mom["largest_day_share"] > 0.3
    assert mom["jump_driven"] is False
    assert pipeline._momentum_for_rank(mom) == mom["momentum_12_1m"]
