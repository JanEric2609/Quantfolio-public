"""Discover run 57c8f8b8 (2026-11-10): a spin-off left CTVA's ratios describing
the company before it.

Corteva spun off its seed business on 2026-10-01; Yahoo lists it as a 6.665
split. The price history was adjusted, the trailing ratios were not: the
thesis led with a P/E of 8.2 (P/B 0.33, ROE 4 %).
"""
from __future__ import annotations

import json
from datetime import date

import pandas as pd
from conftest import _memory_db

from app.decision.discover.corporate_action import (
    RECENT_ACTION_DAYS,
    attach_corporate_action,
    corporate_action_text,
    recent_corporate_action,
)
from app.decision.discover.dossier_facts import build_facts
from app.decision.discover.dossier_writer import write_dossier

TODAY = date(2026, 11, 10)


def _splits(*pairs: tuple[str, float]) -> pd.Series:
    return pd.Series(
        [v for _, v in pairs],
        index=pd.DatetimeIndex([d for d, _ in pairs]).tz_localize("America/New_York"),
        name="Stock Splits",
    )


def _ctva(_symbol: str) -> pd.Series:
    return _splits(("2019-06-03", 3.0), ("2026-10-01", 6.665))


def test_the_spin_off_reads_as_a_recent_action():
    assert recent_corporate_action("CTVA", today=TODAY, fetch=_ctva) == {"date": "2026-10-01", "factor": 6.665}


def test_old_or_absent_actions_and_failed_lookups_are_none():
    old = lambda _s: _splits(("2019-06-03", 3.0))  # noqa: E731
    assert recent_corporate_action("X", today=TODAY, fetch=old) is None
    assert recent_corporate_action("X", today=TODAY, fetch=lambda _s: None) is None
    assert recent_corporate_action("X", today=TODAY, fetch=lambda _s: _splits()) is None

    def _boom(_s):
        raise RuntimeError("yahoo down")

    assert recent_corporate_action("X", today=TODAY, fetch=_boom) is None
    edge = date.fromordinal(TODAY.toordinal() - RECENT_ACTION_DAYS - 1).isoformat()
    assert recent_corporate_action("X", today=TODAY, fetch=lambda _s: _splits((edge, 2.0))) is None


def test_attach_hides_the_reported_ratios_and_keeps_market_cap():
    scores = {
        "sentiment_fundamentals": {
            "pe": 8.17, "pb": 0.33, "roe": 0.04, "de_ratio": 19.4, "revenue_growth": 0.037,
            "market_cap": 8.08e9, "analyst_target": 17.0,
        },
        "concerns": ["earnings on 2026-11-11 (in 1 days)"],
    }
    out = attach_corporate_action("CTVA", scores, today=TODAY, fetch=_ctva)
    sf = out["sentiment_fundamentals"]
    assert all(sf[k] is None for k in ("pe", "pb", "roe", "de_ratio", "revenue_growth"))
    assert sf["market_cap"] == 8.08e9 and sf["analyst_target"] == 17.0
    assert sf["corporate_action"] == {"date": "2026-10-01", "factor": 6.665}
    assert out["concerns"][0].startswith("earnings on")
    assert out["concerns"][1].startswith("split or spin-off on 2026-10-01")
    # Run twice (a re-written dossier): one concern, not two.
    again = attach_corporate_action("CTVA", out, today=TODAY, fetch=_ctva)
    assert sum(c.startswith("split or spin-off") for c in again["concerns"]) == 1


def test_no_action_leaves_scores_alone():
    scores = {"sentiment_fundamentals": {"pe": 15.0}}
    assert attach_corporate_action("X", scores, today=TODAY, fetch=lambda _s: None) == {
        "sentiment_fundamentals": {"pe": 15.0}
    }


def test_the_facts_say_why_the_ratios_are_missing():
    text = corporate_action_text({"date": "2026-10-01", "factor": 6.665})
    facts = {
        f.key: f.text
        for f in build_facts(
            {}, annual={}, expected_return_pct=2.8, horizon_months=6,
            fundamentals={"pe": None, "roe": None}, macro=None, corporate_action_text=text,
        )
    }
    assert "pe" not in facts and "roe" not in facts
    assert "2026-10-01" in facts["corporate_action"]
    assert corporate_action_text(None) is None


def test_the_dossier_prompt_has_no_stale_ratio(monkeypatch):
    db = _memory_db()
    seen: list[str] = []

    def _llm(_db, messages, timeout_s=120.0, **_kw):
        seen.append(messages[1]["content"])
        return json.dumps({"thesis": "CTVA had {corporate_action}.", "key_risks": []})

    monkeypatch.setattr("app.decision.discover.dossier_writer._local_llm_sync", _llm)
    # The fundamentals cache still holds the provider's stale figures.
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_fundamentals",
        lambda _symbol, _db: {"pe": 8.17, "pb": 0.33, "roe": 0.04, "de_ratio": 19.38,
                              "revenue_growth": 0.037, "forward_pe": 9.1, "sector": "Basic Materials"},
    )
    monkeypatch.setattr("app.decision.discover.dossier_writer._fetch_sentiment", lambda _symbol, _db: {})
    scores = attach_corporate_action(
        "CTVA",
        {"composite": 0.62, "sentiment_fundamentals": {"pe": 8.17, "pb": 0.33, "sector": "Basic Materials"}},
        today=TODAY, fetch=_ctva,
    )
    candidate = {"symbol": "CTVA", "name": "Corteva", "isin": None, "source": "screen_index",
                 "scores_json": json.dumps(scores), "tradeable_json": "{}"}
    out = write_dossier(db, candidate, {"user_id": "u1"})

    prompt = seen[0]
    for stale in ("8.17", "8.2", "0.33", "19.38", "9.1"):
        assert stale not in prompt, stale
    assert "{corporate_action}: a split or spin-off on 2026-10-01" in prompt
    assert "2026-10-01" in out["dossier"]["thesis"]


def test_the_orchestrator_stores_the_action_on_the_shortlisted_candidate(monkeypatch):
    from app.decision.discover import orchestrator
    from app.foundation.data_backbone import ingest
    from app.foundation.models.entities import DiscoverCandidate, DiscoverRun, User

    db = _memory_db()
    user = User(username="owner", password_hash="x")
    db.add(user)
    db.flush()
    run = DiscoverRun(user_id=user.id, status="running")
    db.add(run)
    db.flush()
    cand = DiscoverCandidate(
        run_id=run.id, symbol="CTVA", name="Corteva", source="screen_index", status="shortlisted",
        scores_json=json.dumps({"sentiment_fundamentals": {"pe": 8.17}}),
    )
    db.add(cand)
    db.commit()
    today = pd.Timestamp.now(tz="America/New_York").normalize() - pd.Timedelta(days=40)
    monkeypatch.setattr(ingest, "_fetch_split_events", lambda _s: pd.Series([6.665], index=[today]))

    orchestrator._attach_corporate_action(db, cand)

    sf = json.loads(cand.scores_json)["sentiment_fundamentals"]
    assert sf["pe"] is None and sf["corporate_action"]["factor"] == 6.665


# --- wording the figures could not catch (run 57c8f8b8, checked against the web) ---

from app.decision.discover.thesis_checks import check_thesis, distinct_risks, tidy_sentence  # noqa: E402


def _check(thesis, risks=(), **kw):
    return check_thesis(thesis, list(risks), facts_text=kw.pop("facts_text", ""), macro=None, direction="long", **kw)


def test_a_holding_named_without_holdings_data_goes():
    check = _check(
        "The fund trails EUNL.DE by a small margin. System expectations project +4.0 % over 6 months "
        "(the system's estimate), exceeding 1.22 % from cash over the same months.",
        ["Overlap: a weekly correlation of 0.86 with the portfolio adds little diversification.",
         "Issuer risk for biotech and healthcare holdings like Moderna"],
        correlation=0.86, is_fund=True,
    )
    assert not any("Moderna" in r for r in check.key_risks)


def test_the_portfolio_style_clause_goes_and_the_figures_stay():
    out = tidy_sentence(
        "SPYY shows a 12-1 month price return of +19.2 % (around the middle of this run's candidates) within a "
        "growth style portfolio but adds little diversification given a weekly correlation of 0.96 with the "
        "portfolio (it adds little diversification)."
    )
    assert "growth style" not in out and out.count("little diversification") == 1 and "+19.2 %" in out


def test_cash_without_the_estimate_and_a_regime_justification_go():
    thesis = (
        "ENX shows a 12-1 month price return of +33.9 % (above the middle of this run's candidates). "
        "Trading at a trailing price-to-earnings ratio of 22.2, it trades above cash yields of 1.22 % from cash "
        "over the same months. The system forecasts +2.4 % over 6 months (the system's estimate)."
    )
    check = _check(thesis, correlation=0.04)
    assert "cash yields" not in check.thesis and "+33.9 %" in check.thesis and "+2.4 %" in check.thesis
    adm = _check(
        "ADM shows a 12-1 month price return of +49.7 % (among the strongest of this run's candidates). "
        "This long stance aligns with the sideways regime model.", correlation=0.2,
    )
    assert "regime" not in adm.thesis


def test_diversification_argued_from_beta_and_a_fall_goes():
    check = _check(
        "ADM shows a 12-1 month price return of +49.7 % (among the strongest of this run's candidates). The "
        "position offers diversification benefits given its modest a market beta of 0.27 against SPY (weekly, 2 "
        "years) and a largest fall of 19 % from a peak (last 13 months).", correlation=0.2,
    )
    assert "diversification" not in check.thesis


def test_insider_buying_and_a_low_beta_are_not_risks_and_repeats_go():
    check = _check(
        "ADM shows a 12-1 month price return of +49.7 % (among the strongest of this run's candidates).",
        [
            "Insider activity: 1 distinct insider(s) opportunistically bought in the trailing 30 days.",
            "Market sensitivity captured by a market beta of 0.29 against VGK (weekly, 2 years)",
            "Overlap: a weekly correlation of 0.82 with the portfolio adds little diversification.",
            "A weekly correlation of 0.82 with the portfolio (it adds little diversification) offers limited "
            "diversification benefits",
            "An annual volatility of 31 % (last 13 months)",
        ],
        correlation=0.82,
    )
    assert check.key_risks == [
        "Overlap: a weekly correlation of 0.82 with the portfolio adds little diversification.",
        "An annual volatility of 31 % (last 13 months)",
    ]
    assert distinct_risks(["a", "A.", "b"]) == ["a", "b"]


def test_insider_selling_and_a_high_beta_stay_risks():
    check = _check(
        "FTI shows a 12-1 month price return of +68.3 % (among the strongest of this run's candidates).",
        ["Insiders sold and bought shares: net insider selling of 5.0 million US dollars.",
         "A market beta of 1.32 against SPY (weekly, 2 years) amplifies market falls."],
    )
    assert len(check.key_risks) == 2


def test_two_figure_phrases_get_their_and():
    assert "15.3 and a return on equity" in tidy_sentence(
        "ABN trades at a trailing price-to-earnings ratio of 15.3 a return on equity of 9.1 %."
    )


def test_a_thesis_of_nothing_but_sloppy_sentences_is_rejected():
    check = _check(
        "The position offers diversification benefits given its modest a market beta of 0.27 against SPY "
        "(weekly, 2 years) and a largest fall of 19 % from a peak (last 13 months). This long stance aligns "
        "with the sideways regime model.", correlation=0.2,
    )
    assert check.reason == "mislabel"


def test_a_dangling_existing_positions_goes():
    assert tidy_sentence(
        "It shows a weekly correlation of 0.82 with the portfolio (it adds little diversification) to existing "
        "positions."
    ).endswith("(it adds little diversification).")
