"""ADR 0017 — Discover scoring integrity (BBVA.MC dossier audit, 2026-09-26).

Covers: analyst consensus that is actually fetched (or visibly missing),
cross-sectional momentum ranks, the per-sector shortlist cap, weekly beta,
the composite contributions breakdown, and the dossier prompt grounding.
"""
from __future__ import annotations

import json
import math
from datetime import date, timedelta
from typing import Any

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.decision.discover import pipeline as pipeline_mod
from app.decision.discover.composite import (
    WEIGHTS,
    composite_contributions,
    compute_weighted_composite,
    derive_signals_from_scores,
)
from app.decision.discover.pipeline import (
    _weekly_beta,
    apply_sector_cap,
    assign_analyst_ranks,
    assign_momentum_ranks,
    sector_cap_for,
    stage_sentiment_fundamentals,
)
from app.foundation.providers.base import MarketDataProvider, provider_result
from app.foundation.providers.registry import ProviderRegistry


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


# ---------------------------------------------------------------------------
# Analyst consensus
# ---------------------------------------------------------------------------


def test_yfinance_analyst_estimates_map_info_payload(monkeypatch):
    import app.foundation.providers.yfinance_provider as mod

    class _FakeTicker:
        def __init__(self, symbol: str) -> None:
            self.info = {
                "currentPrice": 25.2,
                "targetMeanPrice": 23.23864,
                "targetMedianPrice": 23.825,
                "targetHighPrice": 29.0,
                "targetLowPrice": 17.0,
                "numberOfAnalystOpinions": 22,
                "recommendationKey": "hold",
                "recommendationMean": 0.0,  # out-of-scale value seen live; must be ignored
                "currency": "EUR",
            }

    class _FakeYf:
        Ticker = _FakeTicker

    monkeypatch.setitem(__import__("sys").modules, "yfinance", _FakeYf())

    result = mod.YFinanceProvider().get_analyst_estimates("BBVA.MC")

    assert result["ok"] is True
    data = result["data"]
    assert data["target_median"] == pytest.approx(23.825)
    assert data["target_mean"] == pytest.approx(23.23864)
    assert data["current_price"] == pytest.approx(25.2)
    assert data["recommendation"] == "hold"
    assert data["number_analysts"] == 22
    assert "recommendation_mean" not in data


def test_yfinance_analyst_estimates_not_ok_without_coverage(monkeypatch):
    import app.foundation.providers.yfinance_provider as mod

    class _FakeTicker:
        def __init__(self, symbol: str) -> None:
            self.info = {"quoteType": "ETF", "recommendationKey": "none", "targetMeanPrice": 0}

    class _FakeYf:
        Ticker = _FakeTicker

    monkeypatch.setitem(__import__("sys").modules, "yfinance", _FakeYf())

    result = mod.YFinanceProvider().get_analyst_estimates("VUSA.DE")

    assert result["ok"] is False
    assert result["data"]["target_mean"] is None
    assert result["data"]["recommendation"] is None


class _NamedProvider(MarketDataProvider):
    capabilities = {"get_analyst_estimates"}

    def __init__(self, name: str, calls: list[str]) -> None:
        super().__init__()
        self.name = name
        self._calls = calls

    def status(self) -> dict[str, Any]:
        return {"provider": self.name, "enabled": True, "available": True, "message": ""}

    def get_analyst_estimates(self, symbol: str) -> dict[str, Any]:
        self._calls.append(self.name)
        return provider_result(self.name, ok=True, data={"target_median": 1.0, "current_price": 1.0})


def test_registry_asks_yfinance_first_for_analyst_estimates():
    """Finnhub's free tier answers first under the general chain with a
    label and no target; the analyst route puts yfinance first."""
    calls: list[str] = []
    registry = ProviderRegistry([
        _NamedProvider("finnhub", calls),
        _NamedProvider("openbb", calls),
        _NamedProvider("yfinance", calls),
    ])

    result = registry.get_analyst_estimates("BBVA.MC")

    assert result["provider"] == "yfinance"
    assert calls == ["yfinance"]


def _stub_fundamentals(monkeypatch, data: dict[str, Any]) -> None:
    monkeypatch.setattr(
        "app.foundation.market.fundamentals",
        lambda _db, _symbol: {"data": data, "source": "fake", "stale": False},
    )


def _stub_registry(monkeypatch, result: dict[str, Any] | Exception) -> None:
    class _Registry:
        def get_analyst_estimates(self, symbol: str) -> dict[str, Any]:
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr("app.decision.discover.pipeline.build_provider_registry", lambda _db: _Registry())


def test_missing_analyst_data_is_none_with_concern_not_neutral(monkeypatch):
    db = _memory_db()
    _stub_fundamentals(monkeypatch, {"pe_ratio": 10.9, "sector": "Financial Services"})
    _stub_registry(monkeypatch, {"ok": False, "provider": "registry", "data": None, "error": "openbb: throttled"})

    scores, reject = stage_sentiment_fundamentals(db, "BBVA.MC")
    assert scores is not None

    assert reject is None
    assert scores["analyst_estimate_score"] is None
    assert "analyst_unavailable" in scores["concerns"]
    assert scores["sector"] == "Financial Services"
    # ...and the composite leaves the signal out instead of weighting in 0.5.
    assert "analyst" not in derive_signals_from_scores({"sentiment_fundamentals": scores})


def test_analyst_score_prefers_median_target(monkeypatch):
    db = _memory_db()
    _stub_fundamentals(monkeypatch, {"pe_ratio": 10.9})
    _stub_registry(monkeypatch, {
        "ok": True,
        "provider": "yfinance",
        "data": {
            "target_mean": 23.23864, "target_median": 23.825, "target_high": 29.0,
            "current_price": 25.2, "recommendation": "hold", "number_analysts": 22,
        },
    })

    scores, _ = stage_sentiment_fundamentals(db, "BBVA.MC")
    assert scores is not None

    upside = (23.825 - 25.2) / 25.2
    assert scores["analyst_upside"] == pytest.approx(upside, abs=1e-4)
    assert scores["analyst_estimate_score"] == pytest.approx(0.5 + 2 * upside, abs=1e-4)
    assert scores["analyst_count"] == 22
    assert scores["analyst_source"] == "yfinance"
    assert "analyst_unavailable" not in scores["concerns"]
    assert derive_signals_from_scores({"sentiment_fundamentals": scores})["analyst"] == pytest.approx(
        scores["analyst_estimate_score"]
    )


def test_bare_hold_label_is_a_real_neutral_reading(monkeypatch):
    db = _memory_db()
    _stub_fundamentals(monkeypatch, {})
    _stub_registry(monkeypatch, {"ok": True, "provider": "finnhub", "data": {"recommendation": "hold"}})

    scores, _ = stage_sentiment_fundamentals(db, "CINF")
    assert scores is not None

    assert scores["analyst_estimate_score"] == 0.5
    assert "analyst_unavailable" not in scores["concerns"]


def test_funds_skip_analyst_lookup_without_concern(monkeypatch):
    db = _memory_db()
    _stub_fundamentals(monkeypatch, {})
    _stub_registry(monkeypatch, AssertionError("funds must not query analyst consensus"))

    scores, _ = stage_sentiment_fundamentals(db, "VUSA.DE", is_etf=True)
    assert scores is not None

    assert scores["analyst_estimate_score"] is None
    assert "analyst_unavailable" not in scores["concerns"]


# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------


def test_weights_cut_trailing_return_share():
    # 0.95 since the regime signal's weight was dropped (2026-09-28).
    assert sum(WEIGHTS.values()) == pytest.approx(0.95)
    assert WEIGHTS["momentum"] + WEIGHTS["benchmark"] == pytest.approx(0.20)


def test_contributions_sum_to_composite_and_list_every_signal():
    signals = {"momentum": 0.9, "risk": 0.4, "benchmark": 0.7, "portfolio": 0.55, "fundamentals": 0.56}
    rows = composite_contributions(signals, ic_data={"n_obs": 0})

    assert {r["signal"] for r in rows} == set(signals)
    assert sum(float(r["weight"]) for r in rows) == pytest.approx(1.0, abs=1e-3)
    assert sum(float(r["contribution"]) for r in rows) == pytest.approx(
        compute_weighted_composite(signals, ic_data={"n_obs": 0}), abs=1e-3
    )
    contributions = [float(r["contribution"]) for r in rows]
    assert contributions == sorted(contributions, reverse=True)


def test_benchmark_signal_uses_active_t_stat_when_present():
    base = {"backtest_vs_benchmark": {"excess_return_annual": 0.45, "sharpe_delta": 1.0}}
    legacy = derive_signals_from_scores(base)["benchmark"]
    with_t = derive_signals_from_scores(
        {"backtest_vs_benchmark": {**base["backtest_vs_benchmark"], "active_t_stat": 1.0}}
    )["benchmark"]

    assert legacy == pytest.approx(0.97)  # the saturating BBVA.MC-era value
    assert with_t == pytest.approx(0.5 + 0.5 * math.tanh(0.5))


# ---------------------------------------------------------------------------
# Cross-sectional helpers
# ---------------------------------------------------------------------------


def test_momentum_ranks_are_midrank_percentiles():
    score_dicts = [{"momentum_quality": {"momentum_12_1m": m / 100.0}} for m in range(25)]
    score_dicts.append({"momentum_quality": {}})  # no reading: left alone

    assign_momentum_ranks(score_dicts)

    ranks = [sd["momentum_quality"].get("momentum_rank") for sd in score_dicts]
    assert ranks[0] == pytest.approx(0.5 / 25)
    assert ranks[24] == pytest.approx(24.5 / 25)
    assert ranks[-1] is None
    assert ranks[:25] == sorted(ranks[:25])


def test_momentum_ranks_skipped_for_tiny_cross_sections():
    score_dicts = [{"momentum_quality": {"momentum_12_1m": 0.1 * i}} for i in range(5)]
    assign_momentum_ranks(score_dicts)
    assert all("momentum_rank" not in sd["momentum_quality"] for sd in score_dicts)


def _result(symbol: str, sector: str | None, score: float) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "composite_score": score,
        "scores": {"sentiment_fundamentals": {"sector": sector}},
    }


def test_sector_cap_limits_each_sector_and_reports_skips():
    ranked = [_result(f"BANK{i}", "Financial Services", 0.9 - i * 0.01) for i in range(9)]
    ranked += [_result(f"NRG{i}", "Energy", 0.8 - i * 0.01) for i in range(5)]
    ranked += [_result(f"ETF{i}", None, 0.7 - i * 0.01) for i in range(6)]
    ranked += [_result("TECH0", "Technology", 0.6)]

    selected, capped = apply_sector_cap(ranked, 15)

    assert sector_cap_for(15) == 3
    sectors = [pipeline_mod.candidate_sector(r) for r in selected]
    assert sectors.count("Financial Services") == 3
    assert sectors.count("Energy") == 3
    assert sectors.count(None) == 6  # funds are never capped
    assert "TECH0" in {r["symbol"] for r in selected}
    assert len(selected) == 13  # pool exhausted before 15
    assert {r["symbol"] for r in capped} == {f"BANK{i}" for i in range(3, 9)} | {"NRG3", "NRG4"}


def test_sector_cap_counts_already_selected():
    already = [_result("BANK0", "Financial Services", 0.9), _result("BANK1", "Financial Services", 0.9)]
    ranked = [_result("BANK2", "Financial Services", 0.8), _result("BANK3", "Financial Services", 0.7)]

    selected, capped = apply_sector_cap(ranked, 15, already_selected=already)

    assert [r["symbol"] for r in selected] == ["BANK2"]
    assert [r["symbol"] for r in capped] == ["BANK3"]


# ---------------------------------------------------------------------------
# Weekly beta
# ---------------------------------------------------------------------------


def _rows(closes: np.ndarray) -> list[dict[str, Any]]:
    days: list[date] = []
    d = date(2026, 9, 25)
    while len(days) < len(closes):
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    return [{"date": dd.isoformat(), "close": float(c)} for dd, c in zip(days, closes)]


def test_weekly_beta_recovers_beta_that_daily_misses_on_lagged_closes(monkeypatch):
    """Half of each day's market move reaches the candidate one day late (a
    European close before New York): daily beta ~0.5. Weekly returns recover
    all of the lag except across the week boundary (one day in five), so
    their expected beta is ~0.9 against a true 1.0."""
    rng = np.random.default_rng(3)
    n = 520
    bench_r = rng.normal(0.0003, 0.01, n)
    cand_r = 0.5 * bench_r + 0.5 * np.concatenate([[0.0], bench_r[:-1]]) + rng.normal(0, 0.002, n)
    series = {
        "SYNTH": _rows(100 * np.cumprod(1 + cand_r)),
        "SPY": _rows(100 * np.cumprod(1 + bench_r)),
    }
    monkeypatch.setattr(pipeline_mod, "market_history", lambda _db, sym, days=0, **_kw: series[sym])

    from app.foundation.quant_metrics import beta

    weekly = _weekly_beta(None, "SYNTH", "SPY")  # type: ignore[arg-type]
    daily = beta(list(cand_r), list(bench_r))

    assert daily == pytest.approx(0.5, abs=0.1)
    assert weekly is not None
    assert weekly == pytest.approx(0.9, abs=0.12)
    assert weekly > daily + 0.25


def test_weekly_beta_none_on_short_history(monkeypatch):
    short = _rows(np.linspace(100, 110, 100))
    monkeypatch.setattr(pipeline_mod, "market_history", lambda _db, sym, days=0, **_kw: short)
    assert _weekly_beta(None, "SYNTH", "SPY") is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Dossier grounding
# ---------------------------------------------------------------------------


_BBVA_SCORES: dict[str, Any] = {
    "composite": 0.71,
    "quant_signals": {
        "instrument_type": "equity", "market_beta": 0.85, "market_beta_weekly": 1.10,
        "benchmark": "VGK", "factor_betas": {"mkt_rf": 1.33},
    },
    "backtest_vs_benchmark": {
        "candidate_return_annual": 0.5977, "candidate_volatility_annual": 0.28, "window_years": 3.0,
    },
    "momentum_quality": {"momentum_12_1m": 0.6087, "volatility_6m": 0.25},
    "sentiment_fundamentals": {
        "analyst_estimate_score": 0.3909, "analyst_target": 23.825, "analyst_upside": -0.0546,
        "analyst_count": 22, "analyst_recommendation": "hold", "analyst_source": "yfinance",
        "pe": 13.1, "concerns": [],
    },
    "composite_inputs": [
        {"signal": "momentum", "score": 0.99, "weight": 0.1765, "contribution": 0.1747},
        {"signal": "risk", "score": 0.43, "weight": 0.1176, "contribution": 0.0506},
    ],
}


def _mock_llm_capturing(monkeypatch, captured: list[list[dict[str, str]]]) -> None:
    payload = {
        "direction": "long", "conviction": 0.7, "horizon_months": 12,
        "thesis": "Grounded thesis text.", "key_risks": ["Rates"],
    }

    def _fake(_db, messages, timeout_s=120.0, **_kw):
        captured.append(messages)
        return json.dumps(payload)

    monkeypatch.setattr("app.decision.discover.dossier_writer._local_llm_sync", _fake)
    monkeypatch.setattr("app.decision.discover.dossier_writer._fetch_fundamentals", lambda _s, _db: None)
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_sentiment",
        lambda _s, _db: {"news_sentiment": 0.5, "social_mentions": 0, "sentiment_trend": "stable"},
    )


def test_bbva_dossier_is_grounded_and_estimate_starts_from_market_prior(monkeypatch):
    from app.decision.discover.dossier_writer import write_dossier
    from app.foundation.settings import upsert_public_settings

    db = _memory_db()
    upsert_public_settings(db, {"risk_free_rate_pct": 0.0244})
    captured: list[list[dict[str, str]]] = []
    _mock_llm_capturing(monkeypatch, captured)
    candidate = {
        "symbol": "BBVA.MC", "isin": None, "name": "BBVA", "source": "screen_index",
        "scores_json": json.dumps(_BBVA_SCORES), "tradeable_json": "{}",
    }

    result = write_dossier(db, candidate, {"user_id": "user1"})

    prompt = captured[0][1]["content"]
    assert "is the trailing 3-year annualized return" in prompt
    assert "12-1 month momentum is a separate figure: +60.9%" in prompt
    assert "Market beta: 1.10 (weekly returns vs VGK" in prompt
    assert "must not be cited as market beta" in prompt
    assert "median price target 23.825 (-5.5% vs current price)" in prompt
    assert "Composite score drivers" in prompt and "momentum 0.99 x 0.18" in prompt

    dossier = result["dossier"]
    comp = dossier["expected_return_components"]
    beta_adj = 2 / 3 * 1.10 + 1 / 3
    prior = 2.44 + beta_adj * 4.5
    weight = 0.03**2 / (0.03**2 + 0.28**2 / 3.0)
    assert comp["beta"] == pytest.approx(beta_adj, abs=1e-3)
    assert comp["beta_source"] == "weekly"
    assert comp["prior_pct"] == pytest.approx(prior, abs=0.01)
    assert comp["credibility_weight"] == pytest.approx(weight, abs=1e-4)
    expected = prior + weight * (59.77 - prior)
    assert dossier["expected_return"] == pytest.approx(round(expected, 1))
    assert 7.0 < dossier["expected_return"] < 10.0  # was 6.0 (zero-centred shrink)
    assert dossier["composite_breakdown"] == _BBVA_SCORES["composite_inputs"]
    assert dossier["signal_breakdown"]["expected_return_anchor_kind"] == "trailing_3y_annualized_return"
    assert dossier["signal_breakdown"]["analyst_consensus"] == pytest.approx(0.3909)


def test_low_return_long_pick_no_longer_below_cash(monkeypatch):
    """TotalEnergies-style case: a 9%/yr 3y anchor used to shrink to 0.9%,
    below the 2.4% cash rate. From the market-implied prior it cannot."""
    from app.decision.discover.dossier_writer import _resolve_expected_return
    from app.foundation.settings import upsert_public_settings

    db = _memory_db()
    upsert_public_settings(db, {"risk_free_rate_pct": 0.0244})
    scores = {
        "quant_signals": {"instrument_type": "equity", "market_beta_weekly": 0.7},
        "backtest_vs_benchmark": {
            "candidate_return_annual": 0.09, "candidate_volatility_annual": 0.22, "window_years": 3.0,
        },
    }

    resolved = _resolve_expected_return(db, "user1", "equity", scores, 12, "TTE.PA")

    assert resolved["expected_return_pct"] > 2.44


# ---------------------------------------------------------------------------
# Follow-ups from the prod replay of run 2930a59b
# ---------------------------------------------------------------------------


def test_analyst_upside_ranked_within_sector():
    """Consensus targets sit above prices on average, so a +30% upside in a
    sector where everyone shows +30% is unremarkable (Da & Schaumburg 2011)."""
    banks = [{"sentiment_fundamentals": {"sector": "Financial Services", "analyst_upside": u}}
             for u in (-0.05, 0.05, 0.10, 0.15, 0.20)]
    others = [{"sentiment_fundamentals": {"sector": None, "analyst_upside": 0.30 + i / 100}} for i in range(15)]

    assign_analyst_ranks(banks + others)

    bank_ranks = [b["sentiment_fundamentals"]["analyst_rank"] for b in banks]
    assert bank_ranks == pytest.approx([0.1, 0.3, 0.5, 0.7, 0.9])
    signals = derive_signals_from_scores(
        {"sentiment_fundamentals": {**banks[0]["sentiment_fundamentals"], "analyst_estimate_score": 0.4,
                                    "analyst_source": "yfinance"}}
    )
    assert signals["analyst"] == pytest.approx(0.1)


def test_money_market_risk_is_neutral_not_structural():
    scores = {
        "quant_signals": {"instrument_type": "money_market", "cvar_95_daily": 0.0002},
        "verification_gate": {"volatility": 0.002, "max_drawdown": 0.0003},
    }
    assert derive_signals_from_scores(scores)["risk"] == 0.5
    scores["quant_signals"]["instrument_type"] = "etf"
    assert derive_signals_from_scores(scores)["risk"] > 0.95


def test_sector_falls_back_to_analyst_payload(monkeypatch):
    """US listings resolve fundamentals via Finnhub, which has no sector."""
    db = _memory_db()
    _stub_fundamentals(monkeypatch, {"pe_ratio": 12.0})
    _stub_registry(monkeypatch, {
        "ok": True, "provider": "yfinance",
        "data": {"target_median": 110.0, "current_price": 100.0, "sector": "Healthcare"},
    })

    scores, _ = stage_sentiment_fundamentals(db, "CAH")

    assert scores is not None
    assert scores["sector"] == "Healthcare"
