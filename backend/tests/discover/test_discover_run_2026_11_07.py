"""What the review of Discover run 4b6f5ebf (2026-11-07) found in the dossiers and the shortlist."""
from __future__ import annotations

from app.decision.discover.dossier_writer import (
    _grounding_prompt_lines,
    _reword_concerns_for_prompt,
    deterministic_risks,
    deterministic_template,
    merge_risks,
)
from app.decision.discover.pipeline import (
    apply_sector_cap,
    assign_lane_ranks,
    candidate_industry,
    shortlist_order,
)
from app.decision.discover.thesis_checks import check_thesis, is_false_risk, mislabel_problems, plain_field_names


def _check(thesis, risks=(), **kwargs):
    return check_thesis(thesis, list(risks), facts_text="", macro=None, direction="long", **kwargs)


# --- funds fell to the template ---------------------------------------------------


def test_a_known_field_name_is_put_in_words_not_rejected():
    assert plain_field_names("The momentum_rank is high.") == "The momentum rank is high."
    check = _check("The momentum_rank is high.", ["A market_beta_weekly risk."])
    assert check.ok
    assert check.thesis == "The momentum rank is high."
    assert check.key_risks == ["A weekly market beta risk."]


def test_an_unknown_code_is_still_rejected():
    check = _check("The foo_bar_baz is high.")
    assert check.reason == "mislabel"


def test_a_diversified_fund_is_no_claim_about_the_portfolio():
    assert mislabel_problems("A broadly diversified global fund.", correlation=0.96) == []
    assert mislabel_problems("It diversifies the portfolio.", correlation=0.96)
    assert mislabel_problems("A useful diversifier.", correlation=0.96)


def test_the_noisy_one_month_figure_never_reaches_the_prompt():
    out = _reword_concerns_for_prompt({"momentum_quality": {"trailing_1m_annualized_return": -0.94, "momentum_rank": 0.8}})
    assert out == {"momentum_quality": {"momentum_rank": 0.8}}


def test_the_prompt_warns_when_correlation_rules_out_diversifying():
    assert "does not diversify" in _grounding_prompt_lines({"portfolio_fit": {"correlation": 0.76}}, {})
    assert "does not diversify" not in _grounding_prompt_lines({"portfolio_fit": {"correlation": 0.2}}, {})


FUND_SCORES = {
    "composite": 0.68,
    "momentum_quality": {"momentum_12_1m": 0.479, "momentum_rank": 0.843},
    "portfolio_fit": {"correlation": 0.76},
    "quant_signals": {"market_beta_weekly": 0.95},
}


def test_the_template_says_what_the_data_says():
    dossier = deterministic_template({"symbol": "IS3S.DE"}, FUND_SCORES, {"instrument_type": "etf"})
    assert "+47.9%" in dossier["thesis"]
    assert "percentile 84" in dossier["thesis"]
    assert "0.76" in dossier["thesis"]
    assert "0.95" in dossier["thesis"]
    assert len(dossier["thesis"]) <= 600


def test_a_fund_template_has_no_single_name_or_false_currency_risk():
    risks = deterministic_template({"symbol": "IS3S.DE"}, FUND_SCORES, {"instrument_type": "etf"})["key_risks"]
    joined = " ".join(risks)
    assert "single-name" not in joined
    assert "Currency risk" not in joined
    assert "0.76" in joined


def test_a_stock_template_keeps_its_risks():
    risks = deterministic_template({"symbol": "AAPL"}, {"composite": 0.6}, {"instrument_type": "equity"})["key_risks"]
    joined = " ".join(risks)
    assert "single-name" in joined
    assert "Currency risk" in joined


# --- risk lists -------------------------------------------------------------------


def test_lack_of_alpha_factors_is_no_risk():
    check = _check("A fine thesis.", ["Lack of validated alpha factors in current screening", "Margins may fall."])
    assert check.key_risks == ["Margins may fall."]


def test_our_own_scores_are_no_risk():
    assert is_false_risk("Low fundamentals score of 0.47 relative to momentum")
    assert not is_false_risk("Margins may fall.")


def test_ordinary_debt_is_no_risk():
    assert is_false_risk("High debt-equity ratio of 26.5%", de_ratio=26.5)
    assert is_false_risk("Debt-to-equity ratio of 8.9%", de_ratio=8.9)
    assert not is_false_risk("High leverage at 104.68% debt-to-equity ratio", de_ratio=104.68)
    assert not is_false_risk("High debt-equity ratio", de_ratio=None)


def test_calling_ordinary_debt_high_is_rejected_in_the_thesis():
    assert _check("It carries high debt.", de_ratio=26.5).reason == "mislabel"
    assert _check("It carries high debt.", de_ratio=120.0).ok


def test_a_distant_report_is_no_risk():
    far = {"sentiment_fundamentals": {"next_earnings_date": "2027-01-27", "days_to_earnings": 81}}
    near = {"sentiment_fundamentals": {"next_earnings_date": "2026-11-20", "days_to_earnings": 13}}
    assert deterministic_risks(far, {})[0] == []
    assert deterministic_risks(near, {})[0]
    assert "leave it out of key_risks" in _grounding_prompt_lines(far, {})
    assert "name it as event risk" in _grounding_prompt_lines(near, {})


def test_the_writers_distant_earnings_line_goes_too():
    written = ["Event risk from earnings on 2027-02-02", "Valuation is high."]
    assert merge_risks([], written, drop_earnings=True) == ["Valuation is high."]


def test_every_earnings_and_drawdown_line_counts_once():
    required = [
        "Earnings event: The next earnings report on 2026-11-11 (in 4 days).",
        "Drawdown: fell 39.3% peak to trough in the window measured, past the 30% the quality check allows.",
    ]
    out = merge_risks(required, ["Event risk from earnings on 2026-11-11", "Maximum drawdown of 39.3% exceeds 30% threshold"])
    assert out == required


# --- wording ----------------------------------------------------------------------


def test_revenue_growth_reaches_the_prompt_in_percent():
    assert _reword_concerns_for_prompt({"revenue_growth": 0.834}) == {"revenue_growth_pct": 83.4}
    assert _reword_concerns_for_prompt({"revenue_growth": None}) == {"revenue_growth": None}


def test_one_insider_is_no_cluster():
    assert _reword_concerns_for_prompt({"cluster_buy_score": 1.0}) == {"insider_buyers_30d": 1.0}
    assert "not a cluster" in _grounding_prompt_lines({"insider_signal": {"cluster_buy_score": 1.0}}, {})
    assert _check("Supported by insider cluster buys.", insider_buyers=1.0).reason == "mislabel"
    assert _check("Supported by insider cluster buys.", insider_buyers=3.0).ok
    assert _check("One insider bought.", insider_buyers=1.0).ok


# --- shortlist --------------------------------------------------------------------


def _result(symbol, score, sector=None, industry=None, fit=None, lane="stock"):
    return {
        "symbol": symbol, "composite_score": score, "lane": lane,
        "scores": {
            "sentiment_fundamentals": {"sector": sector, "industry": industry},
            "portfolio_fit": {"fit_score": fit},
        },
    }


def test_a_label_that_repeats_the_sector_is_no_industry():
    assert candidate_industry(_result("KMI", 0.6, "Energy", "Energy")) is None
    assert candidate_industry(_result("UNH", 0.6, "Healthcare", "Health Care")) is None
    assert candidate_industry(_result("TTE.PA", 0.6, "Energy", "Oil & Gas Integrated")) == "Oil & Gas Integrated"
    assert candidate_industry(_result("X", 0.6, None, "Energy")) == "Energy"


def test_coarsely_labelled_names_meet_only_the_sector_cap():
    ranked = [_result(s, 0.6 - i / 100, "Energy", "Energy") for i, s in enumerate(["KMI", "XOM", "VLO", "WMB"])]
    selected, capped = apply_sector_cap(ranked, 15)
    assert [r["symbol"] for r in selected] == ["KMI", "XOM", "VLO"]
    assert [r["symbol"] for r in capped] == ["WMB"]


def test_a_tie_goes_to_the_better_fit_and_ranks_agree():
    spyy = _result("SPYY.DE", 0.5218, fit=0.313, lane="fund")
    xdeq = _result("XDEQ.DE", 0.5218, fit=0.45, lane="fund")
    ranked = sorted([spyy, xdeq], key=shortlist_order, reverse=True)
    assert ranked[0]["symbol"] == "XDEQ.DE"
    assign_lane_ranks(ranked)
    assert xdeq["scores"]["lane_rank"] > spyy["scores"]["lane_rank"]
