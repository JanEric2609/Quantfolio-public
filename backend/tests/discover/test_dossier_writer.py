"""Unit tests for the Discover dossier writer."""
from __future__ import annotations

import json

import pytest
from conftest import _memory_db

from app.foundation.models.entities import DiscoveryPrediction, Recommendation, RecommendationDossier
from app.decision.discover.dossier_writer import (
    DEFAULT_SHRINKAGE,
    MZ_MIN_ROWS_BY_TYPE,
    SHRINKAGE_BY_INSTRUMENT_TYPE,
    DossierOutputModel,
    _coerce_dossier,
    _dampen_trailing_return_for_forward_estimate,
    deterministic_template,
    get_dynamic_shrinkage_factor,
    write_dossier,
)


def _candidate(scores: dict | None = None) -> dict:
    return {
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "isin": "US0378331005",
        "source": "screen_index",
        "scores_json": json.dumps(scores or {"composite": 0.85}),
        "tradeable_json": json.dumps(
            {"teilfreistellung_class": "aktien", "domicile": "US"}
        ),
    }


def test_deterministic_template_output():
    """deterministic_template returns a valid §5 schema dict."""
    scores = {"composite": 0.72}
    signal = {"composite_score": 0.72, "portfolio_fit": 0.6}

    dossier = deterministic_template(_candidate(scores), scores, signal)

    assert dossier["direction"] == "long"
    assert dossier["conviction"] == 0.72
    assert dossier["horizon_months"] == 6
    assert dossier["expected_return"] is None
    assert "AAPL" in dossier["thesis"]
    assert len(dossier["key_risks"]) == 3
    assert dossier["signal_breakdown"] == signal
    assert dossier["estimate"] is True
    assert dossier["not_financial_advice"] is True


def test_deterministic_template_neutral_for_low_score():
    """Composite below 0.5 yields a neutral direction."""
    scores = {"composite": 0.35}
    dossier = deterministic_template(_candidate(scores), scores, {})
    assert dossier["direction"] == "neutral"


def test_dossier_output_model_validates():
    """DossierOutputModel accepts a fully populated valid payload."""
    data = {
        "direction": "long",
        "conviction": 0.85,
        "horizon_months": 6,
        "thesis": "Compelling AI-driven growth narrative.",
        "key_risks": ["Valuation", "Competition"],
        "signal_breakdown": {"composite_score": 0.85},
    }
    model = DossierOutputModel.model_validate(data)
    assert model.direction == "long"
    assert model.conviction == 0.85
    assert model.estimate is True
    assert model.not_financial_advice is True


def test_dossier_output_model_rejects_invalid():
    """DossierOutputModel rejects values with wrong types."""
    invalid_cases = [
        {"direction": "long", "conviction": "high", "horizon_months": 6, "thesis": "x", "key_risks": []},
        {"direction": "long", "conviction": 0.8, "horizon_months": "six", "thesis": "x", "key_risks": []},
        {
            "direction": "long",
            "conviction": 0.8,
            "horizon_months": 6,
            "thesis": "x",
            "key_risks": [],
        },
        {
            "direction": "long",
            "conviction": 0.8,
            "horizon_months": 6,
            "thesis": "x",
            "key_risks": "single risk",
        },
    ]
    for case in invalid_cases:
        with pytest.raises(Exception):
            DossierOutputModel.model_validate(case)


def test_signal_breakdown_extraction(monkeypatch):
    """write_dossier extracts the signal_breakdown from scores_json."""
    db = _memory_db()
    scores = {
        "composite": 0.9,
        "alpha_miner": {
            "exposure_score": 0.81, "ic": 0.12, "icir": 0.45, "n_factors": 1, "panel_size": 150,
            "factors": [{"name": "momentum_12_1", "ic": 0.12, "icir": 0.45, "z": 1.3}],
        },
        "alpha_screener": {"regime_affinity": 0.78, "regime_label": "bullish"},
        "portfolio_fit": {"fit_score": 0.66},
        "sentiment_fundamentals": {
            "sentiment_score": 0.55,
            "fundamentals_score": 0.62,
            "analyst_estimate_score": 0.71,
            "pe": 18.4,
            "social_mentions": 12,
        },
    }
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._local_llm_sync",
        lambda _db, _messages, timeout_s=120.0, **_kw: json.dumps(
            {
                "direction": "long",
                "conviction": 0.9,
                "horizon_months": 6,
                "thesis": "Test.",
                "key_risks": [],
            }
        ),
    )
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_fundamentals",
        lambda _symbol, _db: None,
    )
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_sentiment",
        lambda _symbol, _db: {"news_sentiment": 0.5, "social_mentions": 0, "sentiment_trend": "stable"},
    )

    result = write_dossier(db, _candidate(scores), {"user_id": "user1"})
    data = result["dossier"]

    assert data["signal_breakdown"]["composite_score"] == 0.9
    assert data["signal_breakdown"]["ic"] == 0.12
    assert data["signal_breakdown"]["icir"] == 0.45
    assert data["signal_breakdown"]["factor_exposure"] == 0.81
    assert data["signal_breakdown"]["factor_exposures"][0]["z"] == 1.3
    assert data["signal_breakdown"]["regime_state"] == "bullish"
    assert data["signal_breakdown"]["portfolio_context"] == 0.66
    assert data["signal_breakdown"]["sentiment"] == 0.55
    assert data["signal_breakdown"]["fundamentals"] == 0.62
    assert data["signal_breakdown"]["analyst_consensus"] == 0.71


def test_signal_breakdown_surfaces_recency_penalty(monkeypatch):
    """The recency-penalty detail pipeline.py stores under scores["recency_penalty"]
    must surface in the dossier's signal_breakdown, so a familiar name's lower
    score is explained rather than silent."""
    db = _memory_db()
    scores = {
        "composite": 0.4,
        "recency_penalty": {
            "penalty_fraction": 0.2154,
            "days_since_last": 3.2,
            "exempt_reason": None,
        },
    }
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._local_llm_sync",
        lambda _db, _messages, timeout_s=120.0, **_kw: json.dumps(
            {
                "direction": "long", "conviction": 0.4, "horizon_months": 6,
                "thesis": "Test.", "key_risks": [],
            }
        ),
    )
    monkeypatch.setattr("app.decision.discover.dossier_writer._fetch_fundamentals", lambda _s, _d: None)
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_sentiment",
        lambda _s, _d: {"news_sentiment": 0.5, "social_mentions": 0, "sentiment_trend": "stable"},
    )

    result = write_dossier(db, _candidate(scores), {"user_id": "user1"})
    data = result["dossier"]

    assert data["signal_breakdown"]["recency_penalty_applied"] == 0.2154
    assert data["signal_breakdown"]["days_since_last_recommended"] == 3.2
    assert "recency_penalty_exempt_reason" not in data["signal_breakdown"]


def test_signal_breakdown_shows_exempt_reason_when_penalty_waived(monkeypatch):
    db = _memory_db()
    scores = {
        "composite": 0.9,
        "recency_penalty": {
            "penalty_fraction": 0.0,
            "days_since_last": None,
            "exempt_reason": "strong_conviction",
        },
    }
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._local_llm_sync",
        lambda _db, _messages, timeout_s=120.0, **_kw: json.dumps(
            {
                "direction": "long", "conviction": 0.9, "horizon_months": 6,
                "thesis": "Test.", "key_risks": [],
            }
        ),
    )
    monkeypatch.setattr("app.decision.discover.dossier_writer._fetch_fundamentals", lambda _s, _d: None)
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_sentiment",
        lambda _s, _d: {"news_sentiment": 0.5, "social_mentions": 0, "sentiment_trend": "stable"},
    )

    result = write_dossier(db, _candidate(scores), {"user_id": "user1"})
    data = result["dossier"]

    assert data["signal_breakdown"]["recency_penalty_exempt_reason"] == "strong_conviction"


def test_money_market_anchor_prefers_trailing_1m_over_3y_cagr(monkeypatch):
    """Money-market candidates anchor on trailing_1m, not the 3y CAGR.

    XEON.DE incident: a 3y trailing CAGR (3.0%) blends multiple ECB rate
    regimes and overstates the forward return for a cash-equivalent that
    reprices with the current overnight rate almost immediately.
    """
    db = _memory_db()
    scores = {
        "composite": 0.6,
        "quant_signals": {"instrument_type": "money_market"},
        "backtest_vs_benchmark": {"candidate_return_annual": 0.0301},
        "momentum_quality": {"momentum_12_1m": 0.0185, "trailing_1m_annualized_return": 0.021},
    }
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._local_llm_sync",
        lambda _db, _messages, timeout_s=120.0, **_kw: json.dumps(
            {
                "direction": "long",
                "conviction": 0.6,
                "horizon_months": 6,
                "thesis": "Test.",
                "key_risks": [],
            }
        ),
    )
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_fundamentals",
        lambda _symbol, _db: None,
    )
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_sentiment",
        lambda _symbol, _db: None,
    )

    result = write_dossier(db, _candidate(scores), {"user_id": "user1"})
    data = result["dossier"]

    assert data["signal_breakdown"]["expected_return_anchor_pct"] == pytest.approx(2.1)


def test_momentum_anchor_horizon_is_capped_at_one_month_not_scaled_up():
    """momentum_12_1m predicts the next ONE month (Jegadeesh-Titman); a longer
    stated horizon must not multiply the predicted return, unlike the two
    annualized-rate anchors. See docs/archive/plans/momentum-anchor-horizon-issue.md.

    shrinkage_factor is pinned explicitly (rather than relying on the
    default) since this test is about horizon-scaling behaviour, which is
    orthogonal to which per-instrument_type shrinkage constant applies."""
    anchor_pct = 12.0  # 12% momentum reading

    one_month = _dampen_trailing_return_for_forward_estimate(
        anchor_pct, 1, "momentum_12_1m", shrinkage_factor=0.3
    )
    twelve_months = _dampen_trailing_return_for_forward_estimate(
        anchor_pct, 12, "momentum_12_1m", shrinkage_factor=0.3
    )

    assert one_month == twelve_months == pytest.approx(0.3, abs=1e-9)


def test_trailing_return_anchors_still_scale_with_horizon():
    """Non-momentum anchors are annualized rates: scaling by horizon fraction
    is coherent and must be unaffected by the momentum-specific cap."""
    anchor_pct = 12.0

    one_month = _dampen_trailing_return_for_forward_estimate(
        anchor_pct, 1, "trailing_3y_annualized_return", shrinkage_factor=0.3
    )
    twelve_months = _dampen_trailing_return_for_forward_estimate(
        anchor_pct, 12, "trailing_3y_annualized_return", shrinkage_factor=0.3
    )

    assert one_month == pytest.approx(0.3, abs=1e-9)
    assert twelve_months == pytest.approx(3.6, abs=1e-9)
    assert twelve_months > one_month


# ---------------------------------------------------------------------------
# Characterization pins (discover-maths audit fix phase, M5 guard-rail).
# These pin the CURRENT linear dampening behaviour — the audit blueprint's
# "geometric ((1+g)^h - 1) conversion" does not exist in code and must NOT be
# introduced silently. If any of these fail after a change, the change altered
# user-facing expected-return semantics without an explicit decision.
# ---------------------------------------------------------------------------


def test_dampen_clamps_to_plus_minus_25_percentage_points():
    """Extreme anchors saturate at ±25pp regardless of inputs (dossier_writer.py:563)."""
    huge_positive = _dampen_trailing_return_for_forward_estimate(
        6000.0, 12, "trailing_3y_annualized_return", shrinkage_factor=0.3
    )
    huge_negative = _dampen_trailing_return_for_forward_estimate(
        -6000.0, 12, "trailing_3y_annualized_return", shrinkage_factor=0.3
    )

    assert huge_positive == pytest.approx(25.0)
    assert huge_negative == pytest.approx(-25.0)


def test_dampen_rounds_to_one_decimal_place():
    """Output is displayed to users at 1dp precision (round(..., 1) contract)."""
    result = _dampen_trailing_return_for_forward_estimate(
        1.04, 12, "trailing_3y_annualized_return", shrinkage_factor=0.3
    )

    assert result == pytest.approx(round(1.04 * 0.3 * 1.0, 1))
    assert result == pytest.approx(0.3)


def test_dampen_none_anchor_returns_none():
    """No anchor must stay 'no data' — never coerced to 0.0 (flat return)."""
    assert _dampen_trailing_return_for_forward_estimate(None, 6) is None


def test_money_market_anchor_falls_back_to_momentum_12_1m(monkeypatch):
    """Money-market candidate with no trailing_1m signal falls back to momentum_12_1m, never the 3y CAGR."""
    db = _memory_db()
    scores = {
        "composite": 0.6,
        "quant_signals": {"instrument_type": "money_market"},
        "backtest_vs_benchmark": {"candidate_return_annual": 0.0301},
        "momentum_quality": {"momentum_12_1m": 0.0185},
    }
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._local_llm_sync",
        lambda _db, _messages, timeout_s=120.0, **_kw: json.dumps(
            {
                "direction": "long",
                "conviction": 0.6,
                "horizon_months": 6,
                "thesis": "Test.",
                "key_risks": [],
            }
        ),
    )
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_fundamentals",
        lambda _symbol, _db: None,
    )
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_sentiment",
        lambda _symbol, _db: None,
    )

    result = write_dossier(db, _candidate(scores), {"user_id": "user1"})
    data = result["dossier"]

    assert data["signal_breakdown"]["expected_return_anchor_pct"] == pytest.approx(1.8)


def test_equity_anchor_still_prefers_trailing_3y_cagr(monkeypatch):
    """Non-money-market candidates keep the pre-existing 3y CAGR preference."""
    db = _memory_db()
    scores = {
        "composite": 0.6,
        "quant_signals": {"instrument_type": "equity"},
        "backtest_vs_benchmark": {"candidate_return_annual": 0.15},
        "momentum_quality": {"momentum_12_1m": 0.05, "trailing_1m_annualized_return": 0.6},
    }
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._local_llm_sync",
        lambda _db, _messages, timeout_s=120.0, **_kw: json.dumps(
            {
                "direction": "long",
                "conviction": 0.6,
                "horizon_months": 6,
                "thesis": "Test.",
                "key_risks": [],
            }
        ),
    )
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_fundamentals",
        lambda _symbol, _db: None,
    )
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_sentiment",
        lambda _symbol, _db: None,
    )

    result = write_dossier(db, _candidate(scores), {"user_id": "user1"})
    data = result["dossier"]

    assert data["signal_breakdown"]["expected_return_anchor_pct"] == pytest.approx(15.0)


def test_coerce_dossier_fills_missing():
    """_coerce_dossier fills every missing §5 field with a safe default."""
    raw = {"direction": "short"}
    coerced = _coerce_dossier(raw)

    assert coerced["direction"] == "short"
    assert coerced["conviction"] == 0.5
    assert coerced["horizon_months"] == 6
    assert coerced["thesis"] == ""
    assert coerced["key_risks"] == []
    assert coerced["signal_breakdown"] == {}
    assert coerced["estimate"] is True
    assert coerced["not_financial_advice"] is True


def test_write_dossier_llm_success(monkeypatch):
    """A successful LLM response is validated, persisted, and returned."""
    db = _memory_db()
    llm_payload = {
        "direction": "long",
        "conviction": 0.75,
        "horizon_months": 6,
        "thesis": "Buy AAPL on AI momentum",
        "key_risks": ["Valuation risk"],
    }
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._local_llm_sync",
        lambda _db, _messages, timeout_s=120.0, **_kw: json.dumps(llm_payload),
    )
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_fundamentals",
        lambda _symbol, _db: None,
    )
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_sentiment",
        lambda _symbol, _db: {"news_sentiment": 0.5, "social_mentions": 0, "sentiment_trend": "stable"},
    )

    result = write_dossier(db, _candidate(), {"user_id": "user1"})

    dossier = db.get(RecommendationDossier, result["dossier_id"])
    rec = db.get(Recommendation, result["recommendation_id"])
    assert dossier is not None
    assert rec is not None
    assert rec.ticker == "AAPL"
    assert rec.mode == "discover"
    stored = json.loads(dossier.dossier_json)
    assert stored["direction"] == "long"
    assert stored["conviction"] == 0.85
    assert stored["thesis"] == "Buy AAPL on AI momentum"
    assert stored["signal_breakdown"]["composite_score"] == 0.85
    assert stored["estimate"] is True
    assert stored["not_financial_advice"] is True


def test_write_dossier_llm_success_provenance(monkeypatch):
    """A successful LLM dossier is tagged generated_by='llm' with no fallback_reason."""
    db = _memory_db()
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._local_llm_sync",
        lambda _db, _messages, timeout_s=120.0, **_kw: json.dumps(
            {
                "direction": "long",
                "conviction": 0.7,
                "horizon_months": 6,
                "thesis": "Solid fundamentals.",
                "key_risks": [],
            }
        ),
    )
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_fundamentals",
        lambda _symbol, _db: None,
    )
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_sentiment",
        lambda _symbol, _db: {"news_sentiment": 0.5, "social_mentions": 0, "sentiment_trend": "stable"},
    )

    result = write_dossier(db, _candidate(), {"user_id": "user1"})

    assert result["generated_by"] == "llm"
    assert result["fallback_reason"] is None
    stored = json.loads(db.get(RecommendationDossier, result["dossier_id"]).dossier_json)
    assert stored["generated_by"] == "llm"
    assert "fallback_reason" not in stored


def test_write_dossier_skip_llm_never_calls_llm(monkeypatch):
    """skip_llm=True goes straight to the template without any LLM call."""
    db = _memory_db()

    def _must_not_be_called(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("LLM must not be called when skip_llm=True")

    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._local_llm_sync",
        _must_not_be_called,
    )
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_fundamentals",
        lambda _symbol, _db: None,
    )
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_sentiment",
        lambda _symbol, _db: {"news_sentiment": 0.5, "social_mentions": 0, "sentiment_trend": "stable"},
    )

    result = write_dossier(db, _candidate(), {"user_id": "user1"}, skip_llm=True)

    assert result["generated_by"] == "fallback"
    assert result["fallback_reason"].startswith("llm_skipped")
    stored = json.loads(db.get(RecommendationDossier, result["dossier_id"]).dossier_json)
    assert stored["generated_by"] == "fallback"
    assert stored["fallback_reason"].startswith("llm_skipped")


def test_write_dossier_llm_fallback(monkeypatch):
    """If the LLM call fails, write_dossier falls back to the deterministic template."""
    db = _memory_db()

    def _explode(*_args, **_kwargs):
        raise RuntimeError("LLM unavailable")

    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._local_llm_sync",
        _explode,
    )
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_fundamentals",
        lambda _symbol, _db: None,
    )
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_sentiment",
        lambda _symbol, _db: {"news_sentiment": 0.5, "social_mentions": 0, "sentiment_trend": "stable"},
    )

    result = write_dossier(db, _candidate(), {"user_id": "user1"})

    dossier = db.get(RecommendationDossier, result["dossier_id"])
    assert dossier is not None
    stored = json.loads(dossier.dossier_json)
    assert stored["direction"] == "long"
    assert stored["conviction"] == 0.85
    assert "pipeline scores" in stored["thesis"]
    assert stored["key_risks"]
    assert stored["signal_breakdown"]["composite_score"] == 0.85
    assert stored["estimate"] is True


def test_write_dossier_expected_return_is_never_taken_from_llm(monkeypatch):
    """Prod incident (2026-08-20): XEON.DE's dossier had
    expected_return_anchor_pct=3.0 (an ANNUALIZED 3y CAGR) and
    horizon_months=3, but the live LLM echoed expected_return=2.5 —
    implying ~10%/yr for an instrument mechanically bound to the ECB
    deposit rate. Since Phase 3, the LLM's schema no longer includes
    expected_return at all — even if a model emits it anyway (schema
    violation), write_dossier always overwrites it with the value derived
    deterministically from the anchor via
    _dampen_trailing_return_for_forward_estimate, applied uniformly to both
    the LLM and deterministic-fallback paths."""
    db = _memory_db()
    llm_payload = {
        "direction": "long",
        "conviction": 0.70,
        "horizon_months": 3,
        "expected_return": 2.5,  # schema violation: model must not produce this
        "thesis": "XEON.DE tracks EUR overnight rates; anchor likely persists near-term.",
        "key_risks": ["Interest rate volatility"],
    }
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._local_llm_sync",
        lambda _db, _messages, timeout_s=120.0, **_kw: json.dumps(llm_payload),
    )
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_fundamentals",
        lambda _symbol, _db: None,
    )
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_sentiment",
        lambda _symbol, _db: {"news_sentiment": 0.5, "social_mentions": 0, "sentiment_trend": "stable"},
    )
    scores = {
        "composite": 0.7008,
        "quant_signals": {"instrument_type": "money_market"},
        "backtest_vs_benchmark": {"candidate_return_annual": 0.0301},
        # money_market's anchor priority is trailing_1m_annualized_return then
        # momentum_12_1m (see expected_return.py:_ANCHOR_PRIORITY) — neither
        # the 3y CAGR above is used for this instrument_type.
        "momentum_quality": {"momentum_12_1m": 0.03},
    }
    candidate = _candidate(scores)
    candidate["symbol"] = "XEON.DE"

    result = write_dossier(db, candidate, {"user_id": "user1"})

    dossier = db.get(RecommendationDossier, result["dossier_id"])
    stored = json.loads(dossier.dossier_json)
    assert stored["generated_by"] == "llm"
    # ADR 0017: prior = cash 2.2%/yr over the 3mo horizon (0.55), plus the
    # money_market weight 0.90 of the momentum_12_1m anchor's gap to it
    # (3.0 - 2.2), capped at a 1mo horizon: 0.72 / 12 = 0.06 -> 0.6.
    # The old zero-centred shrink gave 0.2, i.e. 0.9%/yr for a fund bound
    # to a ~2% policy rate.
    assert stored["expected_return"] != 2.5
    assert stored["expected_return"] == pytest.approx(0.6, abs=0.05)
    assert stored["not_financial_advice"] is True


def test_write_dossier_expected_return_matches_deterministic_fallback_formula(monkeypatch):
    """The LLM and deterministic-fallback paths derive expected_return from
    the exact same anchor-dampening formula — there is only one place it's
    computed, regardless of generated_by."""
    db = _memory_db()
    scores = {
        "composite": 0.6,
        "backtest_vs_benchmark": {"candidate_return_annual": 0.20},
    }
    llm_payload = {
        "direction": "long",
        "conviction": 0.6,
        "horizon_months": 6,
        "thesis": "Strong momentum expected to partially persist.",
        "key_risks": [],
    }
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._local_llm_sync",
        lambda _db, _messages, timeout_s=120.0, **_kw: json.dumps(llm_payload),
    )
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_fundamentals",
        lambda _symbol, _db: None,
    )
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_sentiment",
        lambda _symbol, _db: None,
    )

    llm_result = write_dossier(db, _candidate(scores), {"user_id": "user1"})
    fallback_dossier = deterministic_template(
        _candidate(scores), scores, llm_result["dossier"]["signal_breakdown"]
    )

    assert llm_result["dossier"]["expected_return"] == pytest.approx(
        fallback_dossier["expected_return"]
    )


def test_llm_refusal_is_not_accepted_as_a_dossier():
    """A refusal with a stray object must fall through to the deterministic template.

    The embedded-object extractor lifts the first {...} out of any prose, and
    _coerce_dossier then fills in every required field — so this validated
    cleanly and was persisted as generated_by="llm" with an empty thesis, no
    fallback_reason, and an expected_return attached as if analysis backed it.
    """
    from app.decision.discover.dossier_writer import _try_parse_json

    assert _try_parse_json('I cannot analyse this candidate. {"error": "insufficient data"}') is None
    assert _try_parse_json("Sorry, I don't have enough data. {}") is None
    # A real dossier still parses out of exactly the same prose-wrapped shape.
    assert _try_parse_json('Here it is: {"thesis": "real", "key_risks": []}') is not None


def test_stale_prompt_emitting_expected_return_range_is_silently_dropped():
    """Seeded prompt_template rows keep asking for a key the schema no longer has.

    `expected_return_range` was removed because the LLM's range was rendered
    beside the anchored, dampened expected_return and could exclude it, and
    because LLM-produced prediction intervals measurably under-cover. Existing
    config rows still carry the old prompt until someone acts on the Stale
    badge, so a model answering that prompt keeps emitting the key. Pydantic's
    `extra="ignore"` must drop it rather than raising — a ValidationError here
    would send every such dossier down the fallback path.
    """
    coerced = _coerce_dossier(
        {
            "direction": "long",
            "conviction": 0.7,
            "horizon_months": 6,
            "expected_return_range": [8.0, 15.0],
            "thesis": "Trailing rally is unlikely to repeat at the same rate.",
            "key_risks": ["Multiple compression"],
            "signal_breakdown": {},
        }
    )
    model = DossierOutputModel(**coerced)
    assert "expected_return_range" not in model.model_dump()


# ---------------------------------------------------------------------------
# F1/F16 — asset-class-specific shrinkage
# ---------------------------------------------------------------------------

def test_money_market_shrinkage_much_shallower_than_equity():
    """money_market's anchor is a persistent policy rate, so it's shrunk
    much less than equity/etf/bond's mean-reverting trailing returns."""
    assert SHRINKAGE_BY_INSTRUMENT_TYPE["money_market"] == pytest.approx(0.90)
    assert SHRINKAGE_BY_INSTRUMENT_TYPE["equity"] == pytest.approx(0.10)
    assert SHRINKAGE_BY_INSTRUMENT_TYPE["etf"] == pytest.approx(0.10)
    assert SHRINKAGE_BY_INSTRUMENT_TYPE["bond"] == pytest.approx(0.10)


def test_dampen_uses_shrinkage_factor_directly():
    """The dampening function is agnostic to instrument_type — it just
    applies whatever shrinkage_factor the caller resolved."""
    money_market_result = _dampen_trailing_return_for_forward_estimate(
        10.0, 12, "trailing_3y_annualized_return", shrinkage_factor=0.90
    )
    equity_result = _dampen_trailing_return_for_forward_estimate(
        10.0, 12, "trailing_3y_annualized_return", shrinkage_factor=0.10
    )
    assert money_market_result == pytest.approx(9.0)
    assert equity_result == pytest.approx(1.0)
    assert money_market_result > equity_result


def test_deterministic_template_uses_instrument_type_shrinkage_without_db():
    """deterministic_template resolves shrinkage from the hardcoded table
    when no db/user_id is supplied (dynamic fit skipped)."""
    scores = {"composite": 0.6}
    signal_breakdown = {
        "expected_return_anchor_pct": 10.0,
        "instrument_type": "money_market",
    }
    dossier = deterministic_template({"symbol": "XEON.DE"}, scores, signal_breakdown)
    # horizon 6mo, prior = cash 2.2 (fallback rate), weight 0.90:
    # (2.2 + 0.90 * (10.0 - 2.2)) * 0.5 = 4.61 -> 4.6
    assert dossier["expected_return"] == pytest.approx(4.6)


def test_deterministic_template_defaults_to_equity_shrinkage():
    scores = {"composite": 0.6}
    signal_breakdown = {"expected_return_anchor_pct": 10.0}  # no instrument_type key
    dossier = deterministic_template({"symbol": "AAPL"}, scores, signal_breakdown)
    # horizon 6mo, prior 2.2 + 1.0 * 4.5 = 6.7, default equity cap 0.10:
    # (6.7 + 0.10 * (10.0 - 6.7)) * 0.5 = 3.5
    assert dossier["expected_return"] == pytest.approx(3.5)


# ---------------------------------------------------------------------------
# F1/F16 — dormant dynamic MZ-slope fitting path
# ---------------------------------------------------------------------------

def _seed_resolved_prediction(
    db, *, user_id: str, instrument_type: str, expected_return: float, realised_return: float, idx: int,
) -> None:
    import uuid
    from datetime import UTC, datetime

    db.add(DiscoveryPrediction(
        id=str(uuid.uuid4()),
        user_id=user_id,
        run_id=f"run-{idx}",
        symbol=f"SYM{idx}",
        predicted_at=datetime.now(UTC),
        horizon_days=180,
        resolve_at=datetime.now(UTC),
        outcome_status="resolved",
        expected_return=expected_return,
        realised_return=realised_return,
        # The shape orchestrator step 4b writes (type under quant_signals).
        features_json={"signal_breakdown": {"quant_signals": {"instrument_type": instrument_type}}},
    ))
    db.commit()


class TestDynamicShrinkageFactor:
    def test_falls_back_to_hardcoded_below_min_rows(self):
        """Dormant by design: with fewer than MZ_MIN_ROWS_BY_TYPE resolved
        rows for a class, the hardcoded constant is used untouched."""
        db = _memory_db()
        for i in range(5):
            _seed_resolved_prediction(
                db, user_id="u1", instrument_type="equity",
                expected_return=1.0, realised_return=1.0, idx=i,
            )
        factor = get_dynamic_shrinkage_factor(db, "u1", "equity")
        assert factor == SHRINKAGE_BY_INSTRUMENT_TYPE["equity"]

    def test_no_resolved_rows_at_all_falls_back(self):
        db = _memory_db()
        factor = get_dynamic_shrinkage_factor(db, "u1", "money_market")
        assert factor == SHRINKAGE_BY_INSTRUMENT_TYPE["money_market"]

    def test_activates_once_min_rows_reached_and_clamps_to_unit_interval(self):
        """Once enough resolved rows with expected_return exist for a class,
        the dynamic MZ-slope fit takes over — a perfectly-calibrated
        synthetic series (realised == expected) should fit close to slope 1."""
        db = _memory_db()
        min_rows = MZ_MIN_ROWS_BY_TYPE["equity"]
        for i in range(min_rows):
            _seed_resolved_prediction(
                db, user_id="u1", instrument_type="equity",
                expected_return=float(i + 1), realised_return=float(i + 1), idx=i,
            )
        factor = get_dynamic_shrinkage_factor(db, "u1", "equity")
        assert factor == pytest.approx(1.0, abs=0.05)

    def test_only_matching_instrument_type_counted_toward_threshold(self):
        """Rows for a different instrument_type must not count toward this
        class's min_rows threshold."""
        db = _memory_db()
        min_rows = MZ_MIN_ROWS_BY_TYPE["money_market"]
        for i in range(min_rows):
            _seed_resolved_prediction(
                db, user_id="u1", instrument_type="equity",
                expected_return=float(i + 1), realised_return=float(i + 1), idx=i,
            )
        factor = get_dynamic_shrinkage_factor(db, "u1", "money_market")
        assert factor == SHRINKAGE_BY_INSTRUMENT_TYPE["money_market"]

    def test_unknown_instrument_type_falls_back_to_default_shrinkage(self):
        db = _memory_db()
        factor = get_dynamic_shrinkage_factor(db, "u1", "not_a_real_type")
        assert factor == DEFAULT_SHRINKAGE


def test_ic_from_a_four_asset_panel_stays_out_of_the_breakdown(monkeypatch):
    """Same gate as the composite: an IC measured across the candidate and
    three benchmark ETFs is not shown as evidence (2026-09-28)."""
    db = _memory_db()
    scores = {
        "composite": 0.6,
        "alpha_miner": {"ic": 0.12, "icir": 0.45, "n_obs": 400, "panel_size": 4},
    }
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._local_llm_sync",
        lambda _db, _messages, timeout_s=120.0, **_kw: json.dumps(
            {"direction": "long", "conviction": 0.6, "horizon_months": 6,
             "thesis": "A test thesis.", "key_risks": []}
        ),
    )
    monkeypatch.setattr("app.decision.discover.dossier_writer._fetch_fundamentals", lambda _s, _db: None)
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_sentiment",
        lambda _s, _db: {"news_sentiment": 0.5, "social_mentions": 0, "sentiment_trend": "stable"},
    )

    data = write_dossier(db, _candidate(scores), {"user_id": "user1"})["dossier"]

    assert "ic" not in data["signal_breakdown"]
    assert "icir" not in data["signal_breakdown"]
