"""What the review of Discover run dd3fe7b9 (2026-11-07) found in the dossiers and the insider signal."""
from __future__ import annotations

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.discover import pipeline
from app.decision.discover.dossier_writer import (
    _grounding_prompt_lines,
    deterministic_risks,
    deterministic_template,
    merge_risks,
    without_false_listing_risk,
)
from app.decision.discover.thesis_checks import (
    check_thesis,
    is_false_risk,
    mislabel_problems,
    without_unsupported_sentences,
)
from app.foundation.core.db import Base
from app.foundation.data_engineering.pit_panel_joins import _one_row_per_trade


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _check(thesis, risks=(), **kwargs):
    return check_thesis(thesis, list(risks), facts_text="", macro=None, direction="long", **kwargs)


# --- a false claim costs its sentence, not the thesis -------------------------------


def test_a_diversification_claim_at_high_correlation_loses_only_its_sentence():
    thesis = "The fund returned a solid figure over the window measured. It offers diversification to the portfolio."
    check = _check(thesis, correlation=0.83)
    assert check.ok
    assert check.thesis == "The fund returned a solid figure over the window measured."


def test_the_same_claim_stays_at_low_correlation():
    thesis = "The stock returned a solid figure over the window measured. It offers diversification to the portfolio."
    check = _check(thesis, correlation=0.2)
    assert check.ok
    assert check.thesis == thesis


def test_a_thesis_of_nothing_but_the_claim_is_still_rejected():
    check = _check("It offers diversification to the portfolio.", correlation=0.83)
    assert check.reason == "mislabel"


def test_an_honest_statement_of_little_diversification_is_kept():
    for wording in ("minimal", "reduced", "limited", "little"):
        thesis = f"The fund overlaps the portfolio and adds {wording} diversification to what is already held."
        assert without_unsupported_sentences(thesis, 0.9) == thesis


def test_a_decimal_does_not_split_a_sentence():
    thesis = "Momentum is +22.9% with a beta of 0.95 over the window measured. It diversifies the portfolio."
    assert without_unsupported_sentences(thesis, 0.7) == "Momentum is +22.9% with a beta of 0.95 over the window measured."


def test_downside_protection_market_implied_and_alpha_factors_go_too():
    thesis = (
        "The stock has a low correlation with the portfolio over the window. "
        "It offers strong downside protection. "
        "The estimate rests on market-implied cash flows. "
        "Low alpha factors add to the picture."
    )
    check = _check(thesis, correlation=0.2)
    assert check.ok
    assert check.thesis == "The stock has a low correlation with the portfolio over the window."
    assert mislabel_problems("It offers strong downside protection.")
    assert mislabel_problems("Low alpha factors.")
    assert mislabel_problems("Based on market-implied cash flows.")


def test_risk_lines_with_those_claims_are_dropped():
    check = _check(
        "The stock has a low correlation with the portfolio over the window.",
        ["Sideways market regime with low alpha factors", "Currency risk for a EUR investor"],
    )
    assert check.key_risks == ["Currency risk for a EUR investor"]


def test_the_prompt_no_longer_calls_the_estimate_market_implied():
    annual = {
        "anchor_kind": "trailing_3y_annualized_return",
        "components": {"anchor_pct": 15.0, "er_annual_pct": 5.0, "prior_pct": 4.5, "risk_free_pct": 2.0,
                       "beta": 1.0, "equity_premium_pct": 2.5, "credibility_weight": 0.05},
    }
    assert "market-implied" not in _grounding_prompt_lines({}, annual)


def test_a_debt_ratio_below_100_is_no_risk():
    assert is_false_risk("Debt ratio of 71.88%", 71.88)
    assert not is_false_risk("Debt ratio of 120%", 120.0)


# --- risks the data shows ---------------------------------------------------------


def test_a_large_recent_fall_is_a_risk_and_reaches_the_prompt():
    scores = {"momentum_quality": {"momentum_12_1m": 3.06, "momentum_rank": 0.99, "fall_from_3m_high": 0.388}}
    _, rest = deterministic_risks(scores, {})
    assert rest == ["Recent fall: the latest close is 38.8% below the highest close of the last 3 months."]
    assert "38.8% below" in _grounding_prompt_lines(scores, {})


def test_a_small_fall_or_an_older_stored_score_is_not():
    assert deterministic_risks({"momentum_quality": {"fall_from_3m_high": 0.14}}, {}) == ([], [])
    assert deterministic_risks({"momentum_quality": {}}, {}) == ([], [])
    assert "below the highest" not in _grounding_prompt_lines({"momentum_quality": {"fall_from_3m_high": 0.1}}, {})


def test_large_net_insider_selling_is_a_risk_in_proportion_to_market_value():
    def scores(flow, cap):
        return {"insider_signal": {"net_insider_flow_usd": flow}, "sentiment_fundamentals": {"market_cap": cap}}

    _, dell = deterministic_risks(scores(-1_023_760_000.0, 236_751_229_636.0), {})
    assert len(dell) == 1
    assert "$1.02bn" in dell[0] and "0.43%" in dell[0]
    # STX and CL in the run: real sales, small against the company.
    assert deterministic_risks(scores(-57_941_141.0, 127_879_312_184.0), {}) == ([], [])
    assert deterministic_risks(scores(-7_212_108.0, 78_506_347_433.0), {}) == ([], [])
    assert deterministic_risks(scores(1_379_490_000.0, 70_000_000_000.0), {}) == ([], [])
    assert deterministic_risks(scores(None, 70_000_000_000.0), {}) == ([], [])
    assert deterministic_risks(scores(-1_000_000_000.0, None), {}) == ([], [])


def test_high_volatility_and_overlap_are_risks_for_any_writer():
    _, rest = deterministic_risks({"verification_gate": {"volatility": 0.7793}, "portfolio_fit": {"correlation": 0.76}}, {})
    assert rest == [
        "Volatility: 77.9% a year, past the 60% the quality check allows.",
        "Overlap: a weekly correlation of 0.76 with the portfolio adds little diversification.",
    ]
    assert deterministic_risks({"verification_gate": {"volatility": 0.59}, "portfolio_fit": {"correlation": 0.49}}, {}) == ([], [])


def test_a_stated_volatility_counts_once_and_other_volatility_stays():
    merged = merge_risks(
        ["Volatility: 76.7% a year, past the 60% the quality check allows."],
        ["elevated 6m volatility at 78.8%", "FX volatility of 12% between EUR and USD", "Oil price volatility"],
    )
    assert merged == [
        "Volatility: 76.7% a year, past the 60% the quality check allows.",
        "FX volatility of 12% between EUR and USD",
        "Oil price volatility",
    ]


def test_a_euro_listing_has_no_listing_currency_risk():
    line = "Currency risk between listing currency and EUR investor base"
    assert without_false_listing_risk("ABI.BR", [line]) == []
    assert without_false_listing_risk("XOM", ["Currency risk between USD listing and EUR investor base"])
    em_line = "FX volatility between EUR and emerging market currencies"
    assert without_false_listing_risk("IS3N.DE", [em_line]) == [em_line]


def test_the_fund_template_has_the_overlap_line_once():
    candidate = {"symbol": "XDEQ.DE"}
    scores = {
        "composite": 0.52,
        "momentum_quality": {"momentum_12_1m": 0.229, "momentum_rank": 0.57},
        "portfolio_fit": {"correlation": 0.93},
        "quant_signals": {"market_beta_weekly": 0.90},
    }
    dossier = deterministic_template(candidate, scores, {"instrument_type": "etf"})
    assert sum(r.startswith("Overlap:") for r in dossier["key_risks"]) == 1
    assert not any("single-name" in r for r in dossier["key_risks"])


# --- recent fall in the momentum stage --------------------------------------------


def test_momentum_stage_stores_the_fall_from_the_3_month_high(monkeypatch):
    closes = [100.0] * 200 + [200.0] * 40 + [150.0] * 20
    rows = [
        {"date": d.date().isoformat(), "close": c}
        for d, c in zip(pd.bdate_range("2025-01-01", periods=len(closes)), closes, strict=True)
    ]
    monkeypatch.setattr(pipeline, "market_history", lambda db, symbol, days: rows)
    monkeypatch.setattr(pipeline, "_without_spike_reversals", lambda db, symbol, df, is_etf=False: (df, 0))
    monkeypatch.setattr(pipeline, "_closes_in_eur", lambda db, symbol, df: (df, "eur"))
    scores, error = pipeline.stage_momentum_quality(_memory_db(), "TEST")
    assert error is None
    assert scores is not None
    assert scores["fall_from_3m_high"] == 0.25


# --- insider trades -----------------------------------------------------------------


def _trade(cik, shares=155479.0, price=176.5, held=1000.0, code="S"):
    return {
        "cik": cik, "filing_date": pd.Timestamp("2026-10-17"), "transaction_date": pd.Timestamp("2026-10-16"),
        "transaction_code": code, "shares": shares, "price_per_share": price, "shares_held_after": held,
    }


def test_a_trade_reported_by_joint_filers_counts_once():
    rows = pd.DataFrame([_trade(f"000{i}") for i in range(13)])
    assert len(_one_row_per_trade(rows)) == 1


def test_two_people_trading_alike_stay_two_when_their_holdings_differ():
    rows = pd.DataFrame([_trade("0001", held=1000.0, code="P"), _trade("0002", held=5000.0, code="P")])
    assert len(_one_row_per_trade(rows)) == 2


def test_separate_trades_of_one_filer_stay_separate():
    rows = pd.DataFrame([_trade("0001", shares=100.0), _trade("0001", shares=200.0)])
    assert len(_one_row_per_trade(rows)) == 2
