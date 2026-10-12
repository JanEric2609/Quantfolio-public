"""The LLM writes words only; its thesis is checked before it is stored (plan 2026-10-12, D7 / WP5)."""
from __future__ import annotations

import json

from conftest import _memory_db

from app.decision.discover.dossier_writer import deterministic_direction, write_dossier
from app.decision.discover.thesis_checks import check_thesis, trim_to_sentence

FACTS = "Composite score: 0.71\nP/E 18.4, ROE 0.22, trailing 3y annualized return 12.3%\nBeta 1.10"
CUTS = {"rate_direction_12m": {"ecb": "cuts", "fed": "cuts"}}
HIKES = {"rate_direction_12m": {"ecb": "hikes", "fed": None}}


def _check(thesis, risks=(), macro=None, direction="long"):
    return check_thesis(thesis, list(risks), facts_text=FACTS, macro=macro, direction=direction)


def test_a_thesis_cut_mid_word_is_trimmed_to_its_last_sentence():
    assert trim_to_sentence("Earnings grew steadily. Margins expand as the compa") == "Earnings grew steadily."
    res = _check("Earnings grew steadily. Margins expand as the compa")
    assert res.ok and res.thesis == "Earnings grew steadily."


def test_a_thesis_with_no_full_sentence_is_rejected():
    assert _check("Margins expand as the compa").reason == "truncated"


def test_cjk_hangul_and_cyrillic_are_rejected():
    assert _check("Strong demand 需求 in the region.").reason == "script"
    assert _check("Solid balance sheet.", ["Риск валюты."]).reason == "script"


def test_a_number_not_in_the_facts_is_rejected():
    res = _check("Trades at consensus 0.27 with upside.")
    assert res.reason == "ungrounded_number" and "0.27" in res.problems[0]
    assert _check("Valued at 102x earnings.").reason == "ungrounded_number"


def test_numbers_from_the_facts_years_and_time_counts_pass():
    # 12.3 % is in the facts; ROE 0.22 may be written as 22 %; 6 months and 2027 are allowed.
    assert _check("A 12.3% trailing return and a 22% ROE over 6 months into 2027.").ok
    assert _check("P/E of 18.4 and beta 1.1.").ok


def test_rate_hikes_with_a_net_cut_macro_block_are_rejected():
    assert _check("Rate hikes will lift bank margins.", macro=CUTS).reason == "macro"


def test_rate_words_without_macro_facts_are_rejected():
    assert _check("Falling rates help the sector.", macro=None).reason == "macro"


def test_rate_words_matching_the_macro_block_pass():
    assert _check("Rate hikes in 2026 lift deposit margins.", macro=HIKES).ok


def test_bearish_wording_contradicts_a_long_direction():
    assert _check("We would avoid the name for now.").reason == "contradiction"
    assert _check("We would avoid the name for now.", direction="neutral").ok
    assert _check("A sell-off in peers lowered the multiple.").ok


def test_direction_is_deterministic():
    assert deterministic_direction(4.0, 1.2, "WATCH") == "long"
    assert deterministic_direction(1.0, 1.2, "WATCH") == "neutral"
    assert deterministic_direction(4.0, 1.2, "HOLD") == "neutral"
    assert deterministic_direction(None, 1.2, "BUY") == "neutral"
    assert deterministic_direction(-3.0, None, "BUY") == "neutral"


def _candidate():
    return {
        "symbol": "AAPL", "name": "Apple Inc.", "isin": "US0378331005", "source": "screen_index",
        "scores_json": json.dumps({"composite": 0.8, "backtest_vs_benchmark": {"candidate_return_annual": 0.20}}),
        "tradeable_json": "{}",
    }


def _stub(monkeypatch, replies):
    calls = []

    def fake(_db, messages, **_kw):
        calls.append(messages)
        return json.dumps(replies[min(len(calls) - 1, len(replies) - 1)])

    monkeypatch.setattr("app.decision.discover.dossier_writer._local_llm_sync", fake)
    monkeypatch.setattr("app.decision.discover.dossier_writer._fetch_fundamentals", lambda _s, _db: None)
    monkeypatch.setattr("app.decision.discover.dossier_writer._fetch_sentiment",
                        lambda _s, _db: {"news_sentiment": 0.5, "social_mentions": 0})
    return calls


def test_a_cjk_token_triggers_a_retry_that_is_then_used(monkeypatch):
    db = _memory_db()
    calls = _stub(monkeypatch, [
        {"direction": "neutral", "conviction": 0.5, "thesis": "Demand 需求 is strong this year.", "key_risks": []},
        {"direction": "short", "conviction": 0.5, "thesis": "Demand is strong this year.", "key_risks": []},
    ])
    out = write_dossier(db, _candidate(), {"user_id": "u1"})
    assert len(calls) == 2
    assert "non-Latin characters" in calls[1][-1]["content"]
    d = out["dossier"]
    assert out["generated_by"] == "llm"
    assert d["thesis"] == "Demand is strong this year."
    assert d["content_retries"] == ["script"]
    # The LLM said neutral, then short; the system's direction stands.
    assert d["direction"] == out["direction"] == "long"
    assert d["horizon_months"] == 6


def test_two_rejections_fall_back_to_the_template_with_the_reason(monkeypatch):
    db = _memory_db()
    _stub(monkeypatch, [
        {"conviction": 0.5, "thesis": "Analysts see consensus 0.27 upside.", "key_risks": []},
    ])
    out = write_dossier(db, _candidate(), {"user_id": "u1"})
    assert out["generated_by"] == "fallback"
    assert out["fallback_reason"] == "ungrounded_number"
    assert out["dossier"]["fallback_reason"] == "ungrounded_number"


def test_skill_horizon_years_follow_two_te_over_alpha_squared():
    from app.decision.discover.dossier_writer import skill_horizon

    out = skill_horizon(0.05, "EUNL.DE")
    assert out is not None
    assert out["years"] == {"alpha_1pct": 100.0, "alpha_2pct": 25.0}
    assert "about 25 years" in out["text"]
    assert skill_horizon(None, "EUNL.DE") is None


def test_stock_dossier_is_labelled_shadow(monkeypatch):
    db = _memory_db()
    _stub(monkeypatch, [{"conviction": 0.5, "thesis": "Demand is strong this year.", "key_risks": []}])
    d = write_dossier(db, _candidate(), {"user_id": "u1"})["dossier"]
    assert d["recommendation_label"] == "Shadow — not a recommendation"
