"""Figures written by code in the dossier (plan 2026-11-08, WP4)."""
from app.decision.discover.dossier_facts import (
    Fact,
    allowed_digit_terms,
    build_facts,
    facts_prompt,
    momentum_descriptor,
    render,
)


def _scores(**over):
    scores = {
        "momentum_quality": {
            "momentum_12_1m": 0.291, "momentum_rank": 0.79, "volatility_6m": 0.22, "max_drawdown": -0.12,
            "fall_from_3m_high": 0.05,
        },
        "backtest_vs_benchmark": {"benchmark": "SPY", "excess_return_annual": 0.031, "candidate_return_years": 3.0},
        "quant_signals": {"market_beta_weekly": 0.88, "market_beta": 1.12, "benchmark": "SPY"},
        "portfolio_fit": {"correlation": 0.43},
        "sentiment_fundamentals": {"analyst_target": 310.0, "analyst_upside": 0.255, "analyst_count": 24},
        "insider_signal": {"cluster_buy_score": 1, "net_insider_flow_usd": -68_800_000},
    }
    scores.update(over)
    return scores


def _facts(scores=None, **kw):
    args = {
        "annual": {"anchor_kind": "trailing_3y_annualized_return", "components": {"anchor_pct": 14.2}},
        "expected_return_pct": 3.2,
        "horizon_months": 6,
        "fundamentals": {"pe": 10.07, "roe": 0.2624, "de_ratio": 71.88, "revenue_growth": 0.04},
        "macro": {"ecb_deposit_rate": {"level_pct": 2.5, "direction_12m": "hikes"},
                  "hicp_euro_area": {"value_pct": 4.0, "month": "2026-10"}},
        "cash_pct": 0.98,
    }
    args.update(kw)
    return {f.key: f.text for f in build_facts(scores or _scores(), **args)}


def test_descriptor_bins_follow_the_strong_momentum_rule():
    assert momentum_descriptor(0.80).startswith("among the strongest")
    # TRV, run 4f3ddca7: rank 0.79 was called "strong" twice by the model.
    assert momentum_descriptor(0.79).startswith("above the middle")
    assert momentum_descriptor(0.5).startswith("around the middle")
    assert momentum_descriptor(0.1).startswith("below the middle")


def test_facts_carry_the_figures_and_their_reading():
    facts = _facts()
    assert facts["momentum"] == "a 12-1 month price return of +29.1 % (above the middle of this run's candidates)"
    assert "0.43" in facts["correlation"] and "low enough to diversify" in facts["correlation"]
    # One market beta, the weekly one (IS3S quoted daily 1.12 and weekly 0.88).
    assert "0.88" in facts["beta"] and "1.12" not in facts["beta"]
    assert "less debt than equity" in facts["debt_equity"]
    assert "not a cluster" in facts["insider_buying"]
    assert facts["insider_flow"] == "net insider selling of 68.8 million US dollars"
    assert "above where prices ended" in facts["analyst_target"]
    assert facts["horizon"] == "6 months"
    assert "hikes" in facts["ecb_rate"]
    # No recent-fall fact below the 20 % line.
    assert "recent_fall" not in facts


def test_measured_figures_come_before_the_analyst_target():
    keys = list(_facts())
    assert keys.index("momentum") < keys.index("analyst_target")
    assert keys.index("correlation") < keys.index("analyst_target")


def test_high_correlation_reads_as_no_diversifier():
    from app.decision.discover.thesis_checks import mislabel_problems

    facts = _facts(_scores(portfolio_fit={"correlation": 0.93}))
    assert "adds little diversification" in facts["correlation"]
    # The phrase itself must pass the checks it is rendered into.
    assert mislabel_problems(f"SPYY has {facts['correlation']}.", correlation=0.93) == []


def test_every_phrase_passes_the_existing_checks():
    from app.decision.discover.thesis_checks import mislabel_problems

    mom = {"momentum_12_1m": 8.48, "momentum_rank": 0.998, "jump_driven": True, "largest_day_return": 1.77,
           "largest_day_share": 0.45, "largest_day_date": "2026-08-19", "fall_from_3m_high": 0.35,
           "volatility_6m": 1.87, "max_drawdown": -0.35}
    for corr in (0.06, 0.93):
        facts = _facts(_scores(momentum_quality=mom, portfolio_fit={"correlation": corr}))
        for key, text in facts.items():
            assert mislabel_problems(
                f"MRNA has {text}.", momentum_rank=0.998, correlation=corr, de_ratio=71.88, insider_buyers=1,
            ) == [], key


def test_fund_facts_leave_out_company_ratios():
    facts = _facts(is_fund=True)
    assert "pe" not in facts and "debt_equity" not in facts


def test_jump_and_recent_fall_are_part_of_the_momentum_phrase():
    # MRNA, run 4f3ddca7: a separate "cite the jump too" instruction was ignored.
    mom = {"momentum_12_1m": 8.48, "momentum_rank": 0.998, "jump_driven": True, "largest_day_return": 1.77,
           "largest_day_share": 0.45, "largest_day_date": "2026-08-19", "fall_from_3m_high": 0.35}
    facts = _facts(_scores(momentum_quality=mom))
    assert "2026-08-19" in facts["momentum"] and "45 %" in facts["momentum"]
    assert "35.0 % below the highest close" in facts["momentum"]
    assert "jump" not in facts and "recent_fall" not in facts


def test_one_volatility_figure_the_gates():
    facts = _facts(_scores(verification_gate={"volatility": 1.8746}))
    assert facts["volatility"] == "an annual volatility of 187 % (last 13 months)"
    assert "6 months" in _facts()["volatility"]


def test_analyst_target_says_above_or_below():
    down = _facts(_scores(sentiment_fundamentals={"analyst_target": 20.0, "analyst_upside": -0.508}))
    assert "50.8 % below the current price" in down["analyst_target"]
    assert "25.5 % above the current price" in _facts()["analyst_target"]


def test_lead_ins_the_phrase_already_carries_are_dropped():
    facts = [
        Fact("correlation", "a weekly correlation of 0.30 with the portfolio (low enough to diversify it)"),
        Fact("volatility", "an annual volatility of 187 % (last 13 months)"),
        Fact("insider_flow", "net insider selling of 68.8 million US dollars"),
        Fact("momentum", "a 12-1 month price return of +39.0 % (above the middle of this run's candidates)"),
        Fact("expected_return", "+3.7 % over 6 months (the system's estimate)"),
        Fact("horizon", "6 months"),
        Fact("pe", "a trailing price-to-earnings ratio of 24.3"),
    ]
    # Model outputs seen on 2026-11-08 against the prod model.
    cases = {
        "{momentum} leads.": "A 12-1 month price return of +39.0 % (above the middle of this run's candidates) leads.",
        "It offers low portfolio overlap via its {correlation}.":
            "It offers low portfolio overlap via a weekly correlation of 0.30 with the portfolio (low enough to "
            "diversify it).",
        "It carries high {volatility}.": "It carries an annual volatility of 187 % (last 13 months).",
        "Significant net insider selling of {insider_flow} suggests caution.":
            "Net insider selling of 68.8 million US dollars suggests caution.",
        "It offers {expected_return} over {horizon}, outpacing cash.":
            "It offers +3.7 % over 6 months (the system's estimate), outpacing cash.",
        "MDLZ offers {correlation} to diversify the portfolio while delivering {expected_return} over the next "
        "six months.":
            "MDLZ offers a weekly correlation of 0.30 with the portfolio (low enough to diversify it) while "
            "delivering +3.7 % over 6 months (the system's estimate).",
        "XDEM has {correlation} with the portfolio. It tracks EUNL.DE closely.":
            "XDEM has a weekly correlation of 0.30 with the portfolio (low enough to diversify it). It tracks EUNL.DE "
            "closely.",
        "HLT has a correlation of {correlation}, yet its high {pe} suggests valuation sensitivity.":
            "HLT has a weekly correlation of 0.30 with the portfolio (low enough to diversify it), yet a trailing "
            "price-to-earnings ratio of 24.3 suggests valuation sensitivity.",
    }
    for raw, expected in cases.items():
        text, problems = render(raw, facts, allowed_digit_terms("TRV"))
        assert problems == []
        assert text == expected


def test_render_substitutes_keys():
    facts = [Fact("momentum", "a +29.1 % return"), Fact("correlation", "a correlation of 0.43")]
    text, problems = render("TRV has {momentum}. It adds {correlation}.", facts, allowed_digit_terms("TRV"))
    assert text == "TRV has a +29.1 % return. It adds a correlation of 0.43."
    assert problems == []


def test_render_drops_a_sentence_that_writes_its_own_figure():
    facts = [Fact("momentum", "a +29.1 % return")]
    text, problems = render(
        "TRV has {momentum}. Insiders sold -$68.8 million. It is an insurer.", facts, allowed_digit_terms("TRV")
    )
    assert text == "TRV has a +29.1 % return. It is an insurer."
    assert len(problems) == 1 and "68.8" in problems[0]


def test_render_drops_a_sentence_with_an_unknown_key():
    text, problems = render("It has {moat}. Fine.", [Fact("momentum", "x")], [])
    assert text == "Fine."
    assert "{moat}" in problems[0]


def test_names_with_digits_are_not_figures():
    terms = allowed_digit_terms("IS3S.DE", "iShares Edge MSCI World Value Factor UCITS ETF", "S&P 500")
    text, problems = render(
        "IS3S tracks the S&P 500 style universe. Its 12-1 month momentum is {m}.", [Fact("m", "a +5.0 % return")],
        terms,
    )
    assert problems == []
    assert "IS3S" in text and "S&P 500" in text


def test_prompt_lists_every_key():
    prompt = facts_prompt([Fact("momentum", "a +29.1 % return"), Fact("beta", "a beta of 0.88")])
    assert "{momentum}: a +29.1 % return" in prompt
    assert "{beta}: a beta of 0.88" in prompt


# --- WP8 (plan 2026-11-10): splices, duplicate risks, one-reason theses, fund style claims ------------


def test_a_key_after_its_own_noun_is_rejected_not_spliced():
    facts = [Fact("volatility", "an annual volatility of 21 %"), Fact("momentum", "a past return of +8.0 %")]
    text, problems = render("It carries annual volatility at {volatility}.", facts, allowed_digit_terms("X"))
    assert text == "" and len(problems) == 1 and "volatility" in problems[0]
    # the model's lead-in the phrase already carries is dropped, as before
    text, problems = render("It has an annualized {momentum}.", facts, allowed_digit_terms("X"))
    assert problems == [] and text == "It has a past return of +8.0 %."
    assert "annualized" not in text


def test_two_different_figures_may_share_a_word():
    facts = [Fact("er", "an expected return of 2.3 %"), Fact("cash", "a cash return of 2.0 %")]
    text, problems = render("The system expects {er}, against {cash}.", facts, allowed_digit_terms("X"))
    assert problems == [] and "2.3" in text and "2.0" in text


def test_fund_style_claims_need_holdings_data():
    from app.decision.discover.thesis_checks import mislabel_problems

    claim = "SPYY gives growth exposure through large-cap US technology."
    assert len(mislabel_problems(claim, "", is_fund=True)) == 2
    assert mislabel_problems(claim, "growth exposure; large-cap US technology", is_fund=True) == []
    assert mislabel_problems(claim, "", is_fund=False) == []
