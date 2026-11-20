"""M5 Option 1: building-block expected-return mode behind er_mode="blocks".

Audit F14 context: trailing 3y CAGR has ~zero out-of-sample predictive value
(Goyal-Welch 2008; Vanguard found R²≈0 at 1y; DeBondt-Thaler reversal implies
the wrong SIGN at ≥2y horizons). Industry ships building-block decompositions
(Vanguard-style: dividend yield + trend growth + valuation reversion). This
module adds that estimator for equities/ETFs and a current-yield path for
money-market instruments, with fixed-credibility shrinkage toward asset-class
grand means.

Oracle corrections implemented here (superseding blueprint M5 Option 1):
- Bühlmann-style FIXED-credibility shrinkage (w pinned at the 0.40 cap) — this
  is NOT James-Stein and must never be described as such;
- CAPE reversion term clamped to ±2pp annualized;
- grand means are annually-refreshed documented constants, never
  runtime-computed (circularity);
- bonds stay on the trailing fallback until YTM ingestion exists.

Rollback contract S-M5b: flipping er_mode back to "trailing" restores the
Option-0 dossiers byte-identically.
"""
from __future__ import annotations

import inspect
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import numpy as np
import pytest
from conftest import _memory_db

from app.foundation.models.entities import PriceCache
from app.decision.discover.dossier_writer import write_dossier
from app.foundation.expected_return_blocks import (
    CAPE_REVERSION_CLAMP_PPTS,
    GRAND_MEAN_ANNUAL,
    SHRINKAGE_W_CAP,
    building_block_er,
)
from app.foundation.settings import get_public_settings, upsert_public_settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_prices(db, ticker: str, closes: list[float]) -> None:
    """Seed PriceCache with business-day closes ending today."""
    days: list[date] = []
    d = date.today()
    while len(days) < len(closes):
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    now = datetime.now(UTC)
    for dd, close in zip(days, closes):
        db.add(PriceCache(
            id=uuid4().hex, ticker=ticker.upper(), date=dd,
            close=Decimal(str(round(close, 4))), fetched_at=now,
            source="test", stale=False, currency="EUR",
        ))
    db.commit()


def _trending_closes(n: int = 420, seed: int = 7) -> list[float]:
    rng = np.random.default_rng(seed)
    steps = [rng.normal(0.004, 0.02 if i % 40 < 10 else 0.008) for i in range(n)]
    return list(100.0 * np.cumprod(np.exp(steps)))


def _candidate(symbol: str, scores: dict) -> dict:
    return {
        "symbol": symbol,
        "name": "Test Instrument",
        "isin": "US0378331005",
        "source": "screen_index",
        "scores_json": json.dumps(scores),
        "tradeable_json": json.dumps({"teilfreistellung_class": "aktien", "domicile": "US"}),
    }


def _mock_llm(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._local_llm_sync",
        lambda _db, _messages, timeout_s=120.0, **_kw: json.dumps(payload),
    )
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_sentiment",
        lambda _symbol, _db: {"news_sentiment": 0.5, "social_mentions": 0, "sentiment_trend": "stable"},
    )


_LLM_PAYLOAD = {
    "direction": "long",
    "conviction": 0.8,
    "horizon_months": 6,
    "thesis": "T.",
    "key_risks": [],
}

_BLOCK_SCORES = {
    "composite": 0.8,
    "quant_signals": {"instrument_type": "equity"},
    "backtest_vs_benchmark": {"candidate_return_annual": 0.20},
}


# ---------------------------------------------------------------------------
# building_block_er — pure function
# ---------------------------------------------------------------------------


def test_equity_components_decompose_independently():
    """Each block is visible in components; er = gm + w*(raw − gm), cape absent."""
    result = building_block_er(
        "equity",
        {"dividend_yield": 0.02, "earnings_growth": 0.05},
        cape_ratio=None,
        market_avg_cape=None,
        current_yield_annual=None,
    )

    gm = GRAND_MEAN_ANNUAL["equity"]
    raw = 0.02 + 0.05 + 0.0
    assert result["components"]["div_yield"] == pytest.approx(0.02)
    assert result["components"]["eps_growth_trend"] == pytest.approx(0.05)
    assert result["components"]["cape_term"] == 0.0
    assert result["components"]["raw"] == pytest.approx(raw)
    assert result["er_annual"] == pytest.approx(gm + SHRINKAGE_W_CAP * (raw - gm))
    assert "cape_unavailable" in result["concerns"]
    assert "building_blocks_incomplete" not in result["concerns"]


def test_cape_reversion_halfway_over_ten_years_clamped():
    """Halfway reversion over 10y, annualized, clamped to ±CAPE_REVERSION_CLAMP_PPTS."""
    overvalued = building_block_er(
        "equity", {"dividend_yield": 0.02, "earnings_growth": 0.05},
        cape_ratio=10.0, market_avg_cape=20.0, current_yield_annual=None,
    )
    # 0.5 * (20/10 − 1) / 10 = +0.05 raw → clamped to +0.02
    assert overvalued["components"]["cape_term"] == pytest.approx(CAPE_REVERSION_CLAMP_PPTS / 100)
    assert "cape_unavailable" not in overvalued["concerns"]

    undervalued = building_block_er(
        "equity", {"dividend_yield": 0.02, "earnings_growth": 0.05},
        cape_ratio=40.0, market_avg_cape=20.0, current_yield_annual=None,
    )
    # 0.5 * (20/40 − 1) / 10 = −0.025 raw → floored at −0.02
    assert undervalued["components"]["cape_term"] == pytest.approx(-CAPE_REVERSION_CLAMP_PPTS / 100)


def test_missing_dividend_or_growth_yields_none_with_concern():
    """Either missing equity component => er None + building_blocks_incomplete."""
    no_div = building_block_er("equity", {"earnings_growth": 0.05}, None, None, None)
    no_growth = building_block_er("equity", {"dividend_yield": 0.02}, None, None, None)

    assert no_div["er_annual"] is None
    assert "building_blocks_incomplete" in no_div["concerns"]
    assert no_growth["er_annual"] is None
    assert "building_blocks_incomplete" in no_growth["concerns"]


def test_money_market_uses_current_yield_never_trailing_cagr():
    """money_market ER comes from the current-yield input alone; the signature has
    no trailing-CAGR parameter at all (structural guarantee, XEON.DE precedent)."""
    params = set(inspect.signature(building_block_er).parameters)
    assert not any("trailing" in p or "cagr" in p for p in params)

    result = building_block_er(
        "money_market", {}, cape_ratio=None, market_avg_cape=None, current_yield_annual=0.021
    )

    gm = GRAND_MEAN_ANNUAL["money_market"]
    assert result["er_annual"] == pytest.approx(gm + SHRINKAGE_W_CAP * (0.021 - gm))

    missing = building_block_er(
        "money_market", {}, cape_ratio=None, market_avg_cape=None, current_yield_annual=None
    )
    assert missing["er_annual"] is None
    assert missing["concerns"]


def test_shrinkage_cap_w_max_040():
    """Extreme raw inputs can never push er beyond gm + 0.40*(raw − gm); w is
    FIXED at the cap (Bühlmann-style fixed credibility, never James-Stein)."""
    result = building_block_er(
        "equity",
        {"dividend_yield": 0.02, "earnings_growth": 0.30},
        cape_ratio=10.0, market_avg_cape=30.0, current_yield_annual=None,
    )

    gm = GRAND_MEAN_ANNUAL["equity"]
    # Spec's hypothetical unclamped upper bound: div .02 + growth .30 + cape .05.
    assert result["er_annual"] <= gm + 0.40 * (0.37 - gm) + 1e-9
    # And w is exactly the fixed cap on the ACTUAL (clamped) raw:
    actual_raw = result["components"]["raw"]
    assert result["er_annual"] == pytest.approx(gm + SHRINKAGE_W_CAP * (actual_raw - gm))
    assert SHRINKAGE_W_CAP == 0.40


def test_bond_defers_with_concern():
    """No YTM ingestion exists — bonds explicitly defer instead of fabricating."""
    result = building_block_er(
        "bond", {"dividend_yield": 0.03, "earnings_growth": 0.05}, None, None, 0.04
    )

    assert result["er_annual"] is None
    assert "bond_ytm_unavailable" in result["concerns"]


# ---------------------------------------------------------------------------
# Dispatcher integration + rollback contract S-M5b
# ---------------------------------------------------------------------------


def _stub_fundamentals(monkeypatch, data: dict) -> None:
    monkeypatch.setattr(
        "app.foundation.market.fundamentals",
        lambda _db, _ticker: {"data": data, "source": "test", "stale": False},
    )


def test_er_mode_blocks_flips_anchor_source_and_back(monkeypatch):
    """er_mode="blocks" switches anchor kind and value; flipping back to
    "trailing" reproduces the baseline dossier byte-identically (S-M5b)."""
    db = _memory_db()
    _seed_prices(db, "BLOCK", _trending_closes())
    _stub_fundamentals(monkeypatch, {"dividend_yield": 0.02, "earnings_growth": 0.05})
    _mock_llm(monkeypatch, _LLM_PAYLOAD)

    baseline = write_dossier(db, _candidate("BLOCK", _BLOCK_SCORES), {"user_id": "user1"})
    assert baseline["dossier"]["expected_return_anchor_kind"] == "trailing_3y_annualized_return"
    # ADR 0017: prior 2.2 + 1.0*4.5 = 6.7; tilt 0.10*(20-6.7) = 1.33; x 6/12
    assert baseline["dossier"]["expected_return"] == pytest.approx(4.0)

    upsert_public_settings(db, {"er_mode": "blocks"})
    blocks = write_dossier(db, _candidate("BLOCK", _BLOCK_SCORES), {"user_id": "user1"})
    b_dossier = blocks["dossier"]

    assert b_dossier["expected_return_anchor_kind"] == "building_blocks_credibility"
    # er_annual = 0.07 + 0.4*(0.07 − 0.07) = 0.07 → 7% * 6/12 = 3.5%
    assert b_dossier["expected_return"] == pytest.approx(3.5)
    assert b_dossier["expected_return"] != baseline["dossier"]["expected_return"]
    # Band still attached in blocks mode, centered on the blocks er_annual:
    assert b_dossier.get("expected_return_band") is not None

    upsert_public_settings(db, {"er_mode": "trailing"})
    rolled_back = write_dossier(db, _candidate("BLOCK", _BLOCK_SCORES), {"user_id": "user1"})
    assert rolled_back["dossier"] == baseline["dossier"]


def test_trailing_default_byte_identical(monkeypatch):
    """er_mode unset vs explicitly "trailing" produce identical dossiers."""
    db = _memory_db()
    _seed_prices(db, "TRAIL", _trending_closes(seed=5))
    _stub_fundamentals(monkeypatch, {"dividend_yield": 0.02, "earnings_growth": 0.05})
    _mock_llm(monkeypatch, _LLM_PAYLOAD)

    unset = write_dossier(db, _candidate("TRAIL", _BLOCK_SCORES), {"user_id": "user1"})
    upsert_public_settings(db, {"er_mode": "trailing"})
    explicit = write_dossier(db, _candidate("TRAIL", _BLOCK_SCORES), {"user_id": "user1"})

    assert explicit["dossier"] == unset["dossier"]
    assert get_public_settings(_memory_db())["er_mode"] == "trailing"


def test_blocks_mode_incomplete_fundamentals_degrade_to_trailing(monkeypatch):
    """blocks mode with unusable fundamentals falls back to the trailing path
    rather than emitting an empty estimate."""
    db = _memory_db()
    _seed_prices(db, "DEGRADE", _trending_closes(seed=9))
    _stub_fundamentals(monkeypatch, {})  # no div/growth → incomplete
    _mock_llm(monkeypatch, _LLM_PAYLOAD)

    upsert_public_settings(db, {"er_mode": "blocks"})
    degraded = write_dossier(db, _candidate("DEGRADE", _BLOCK_SCORES), {"user_id": "user1"})

    assert degraded["dossier"]["expected_return_anchor_kind"] == "trailing_3y_annualized_return"
    assert degraded["dossier"]["expected_return"] == pytest.approx(4.0)  # see baseline above
