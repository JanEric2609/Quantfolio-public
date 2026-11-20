"""M5 Option 0 (backend half): PRIIPs-style P10/P50/P90 return band + er_mode rollback key.

Audit context: discover-maths A8/F14 — the displayed "Expected Return" was pure
linear arithmetic of one backward-looking number presented as forward-looking.
The honest companion (dossier_writer.py house comment) is a Monte-Carlo-style
band around the SAME anchor, leaving the anchor's numeric value and rounding
untouched (characterization pins in tests/discover/test_dossier_writer.py guard
the linear conversion — the geometric conversion is explicitly out of scope).

Blueprint: docs/archive/audits/2026-08-discover-maths/blueprint_appendix.md M5 Option 0,
SUPERSEDED by Oracle corrections: circular BLOCK bootstrap (not iid, not pure
GBM except as short-history fallback), L=21 / B=10000 / H=round(months/12*252),
total-return percentiles (never annualized), log-space anchor centering,
<250 obs → GBM fallback + "wide_band" concern.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import numpy as np
import pytest
from conftest import _memory_db

from app.foundation.models.entities import PriceCache
from app.decision.discover.dossier_writer import (
    _resolve_expected_return,
    deterministic_template,
    write_dossier,
)
from app.foundation.return_band import compute_return_band
from app.foundation.settings import get_public_settings, upsert_public_settings


# ---------------------------------------------------------------------------
# Helpers (business-day seeding mirrors tests/test_discover_pipeline_fixes.py)
# ---------------------------------------------------------------------------


def _seed_prices(db, ticker: str, closes: list[float], end: date | None = None) -> None:
    """Seed PriceCache with business-day closes ending at *end* (today by default)."""
    end = end or date.today()
    days: list[date] = []
    d = end
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


def _trending_closes(n: int = 420, daily_drift: float = 0.004, seed: int = 7) -> list[float]:
    """Strongly-trending closes with two volatility scales (skewed, fat-ish tails).

    The strong drift is deliberate: it makes
    test_band_centering_legacy_raw_anchor_superseded discriminating — an
    UNcentered bootstrap median would sit ~60pp above the anchor-implied total
    return, far outside any plausible tolerance.
    """
    rng = np.random.default_rng(seed)
    steps = np.empty(n)
    for i in range(n):
        vol = 0.02 if i % 40 < 10 else 0.008  # turbulent blocks inside calm drift
        steps[i] = rng.normal(daily_drift, vol)
    return list(100.0 * np.cumprod(np.exp(steps)))


def _quiet_closes(n: int = 120, seed: int = 11) -> list[float]:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0002, 0.01, size=n)
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


# ---------------------------------------------------------------------------
# compute_return_band unit behaviour
# ---------------------------------------------------------------------------


def test_band_ordered_and_tagged():
    """P10 <= P50 <= P90, house safety tags present, block-bootstrap method."""
    db = _memory_db()
    _seed_prices(db, "BANDX", _trending_closes())

    band = compute_return_band(db, "BANDX", 6, 0.08)

    assert band is not None
    assert band["p10"] <= band["p50"] <= band["p90"]
    assert band["estimate"] is True
    assert band["not_tax_advice"] is True
    assert band["method"] == "block_bootstrap"
    assert band["concerns"] == []
    assert isinstance(band["n_paths"], int)


def test_band_centering_legacy_raw_anchor_superseded():
    """Log-space centering maps the band median onto the horizon-converted anchor.

    implied_horizon_total = (1+anchor)^(months/12) - 1; after the constant
    log-shift the median must sit within a small fraction of the band's own
    dispersion (spec: < 2x interquartile width; IQR approximated from the
    P10-P90 span, which upper-bounds ~1.9 IQRs for near-normal shapes).

    O1 decision (discover-maths fix session): this pins compute_return_band's
    UNIT contract only — whatever annual anchor it is handed, the median lands
    on its horizon conversion. At the DOSSIER level the raw-anchor centering
    is superseded by design: _resolve_expected_return now feeds the
    anchor-equivalent of the dampened forward estimate, so end-to-end the
    band median follows the dampened headline (see
    test_band_centered_on_dampened_estimate). Kept as a rename+comment trail,
    not deleted.
    """
    db = _memory_db()
    _seed_prices(db, "CENT", _trending_closes())

    band = compute_return_band(db, "CENT", 6, 0.08)

    assert band is not None
    implied = (1.0 + 0.08) ** (6 / 12) - 1.0
    iqr_proxy = (band["p90"] - band["p10"]) / 1.9
    # Uncentered, this trending series' raw median sits ~60pp above the anchor —
    # only the explicit delta shift can pass this.
    assert abs(band["p50"] - implied) < 2.0 * iqr_proxy
    assert abs(band["p50"] - implied) < 0.05


def test_block_bootstrap_deterministic_given_seed():
    """Identical inputs + identical seed => byte-identical dict."""
    db = _memory_db()
    _seed_prices(db, "DET", _trending_closes(seed=3))

    first = compute_return_band(db, "DET", 6, 0.08, seed=42)
    second = compute_return_band(db, "DET", 6, 0.08, seed=42)

    assert first == second


def test_band_centered_on_dampened_estimate():
    """O1: the dossier band median follows the DAMPENED forward estimate.

    Replay of the C-candidate prod case: trailing 44.5% annualized, equity,
    h=12. ADR 0017: prior 2.2% cash + 1.0 x 4.5% premium = 6.7%, plus 0.10
    of the gap (no volatility on file -> the class cap) = 10.5% headline, so
    the band must center on ~10.5% horizon total — not on the raw
    backward-looking 44.5% anchor it was centered on before the O1 fix.
    """
    db = _memory_db()
    _seed_prices(db, "CANDC", _trending_closes())
    scores = {
        "backtest_vs_benchmark": {"candidate_return_annual": 0.445},
        "momentum_quality": {},
    }

    resolved = _resolve_expected_return(db, None, "equity", scores, 12, "CANDC")

    assert resolved["anchor_kind"] == "trailing_3y_annualized_return"
    assert resolved["expected_return_pct"] == pytest.approx(10.5)
    assert resolved["band"] is not None
    assert resolved["band"]["p50"] == pytest.approx(0.105, abs=0.001)


def test_short_history_gbm_fallback_wide_band():
    """< min_obs returns => GBM fallback via quant_mc calibration + Euler engine,
    flagged with the wide_band concern (short history => unreliable dispersion)."""
    db = _memory_db()
    _seed_prices(db, "SHORT", _quiet_closes(120))

    band = compute_return_band(db, "SHORT", 6, 0.08)

    assert band is not None
    assert band["method"] == "gbm_fallback"
    assert "wide_band" in band["concerns"]
    assert band["p10"] <= band["p50"] <= band["p90"]
    assert band["estimate"] is True
    assert band["not_tax_advice"] is True


def test_no_data_returns_none_gracefully():
    """Empty price table => band omitted from the dossier payload, no exception."""
    db = _memory_db()

    assert compute_return_band(db, "EMPTY", 6, 0.08) is None

    scores = {
        "composite": 0.7,
        "backtest_vs_benchmark": {"candidate_return_annual": 0.08},
    }
    signal_breakdown = {
        "expected_return_anchor_pct": 8.0,
        "expected_return_anchor_kind": "trailing_3y_annualized_return",
        "instrument_type": "equity",
    }
    dossier = deterministic_template(
        _candidate("EMPTY", scores), scores, signal_breakdown, db=db, user_id="user1"
    )
    assert "expected_return_band" not in dossier


# ---------------------------------------------------------------------------
# Dispatcher: both dossier call sites share one code path
# ---------------------------------------------------------------------------


def _mock_llm(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._local_llm_sync",
        lambda _db, _messages, timeout_s=120.0, **_kw: json.dumps(payload),
    )
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_fundamentals",
        lambda _symbol, _db: None,
    )
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_sentiment",
        lambda _symbol, _db: {"news_sentiment": 0.5, "social_mentions": 0, "sentiment_trend": "stable"},
    )


def test_dispatcher_both_call_sites_identical(monkeypatch):
    """LLM path and forced-fallback template path must produce IDENTICAL
    expected_return AND expected_return_band (single dispatcher proof)."""
    db = _memory_db()
    _seed_prices(db, "IDENT", _trending_closes())
    scores = {
        "composite": 0.8,
        "quant_signals": {"instrument_type": "equity"},
        "backtest_vs_benchmark": {"candidate_return_annual": 0.20},
        "momentum_quality": {"momentum_12_1m": 0.05},
    }
    llm_payload = {
        "direction": "long",
        "conviction": 0.8,
        "horizon_months": 6,
        "thesis": "LLM thesis.",
        "key_risks": ["Valuation"],
    }

    _mock_llm(monkeypatch, llm_payload)
    llm_result = write_dossier(db, _candidate("IDENT", scores), {"user_id": "user1"})

    fallback_result = write_dossier(
        db, _candidate("IDENT", scores), {"user_id": "user1"}, skip_llm=True
    )

    llm_dossier = llm_result["dossier"]
    fb_dossier = fallback_result["dossier"]
    assert llm_dossier["expected_return"] == fb_dossier["expected_return"]
    assert llm_dossier.get("expected_return_band") is not None
    assert llm_dossier["expected_return_band"] == fb_dossier["expected_return_band"]
    # Band exists alongside an UNCHANGED point estimate (linear dampening intact):
    # ADR 0017: (6.7 prior + 0.10*(20-6.7)) * 6/12
    assert llm_dossier["expected_return"] == pytest.approx(4.0)


def test_er_mode_invalid_value_falls_back_to_trailing(monkeypatch):
    """er_mode="bogus" normalizes to trailing: expected_return identical to baseline;
    the key itself defaults to "trailing" in public settings."""
    db = _memory_db()
    _seed_prices(db, "MODEX", _trending_closes())
    scores = {
        "composite": 0.8,
        "quant_signals": {"instrument_type": "equity"},
        "backtest_vs_benchmark": {"candidate_return_annual": 0.20},
    }
    llm_payload = {
        "direction": "long",
        "conviction": 0.8,
        "horizon_months": 6,
        "thesis": "T.",
        "key_risks": [],
    }

    assert get_public_settings(_memory_db())["er_mode"] == "trailing"

    _mock_llm(monkeypatch, llm_payload)
    baseline = write_dossier(db, _candidate("MODEX", scores), {"user_id": "user1"})

    upsert_public_settings(db, {"er_mode": "bogus"})
    assert get_public_settings(db)["er_mode"] == "bogus"  # stored verbatim...
    after = write_dossier(db, _candidate("MODEX", scores), {"user_id": "user1"})
    # ...but the dispatcher normalizes it: identical behavior to default.
    assert after["dossier"]["expected_return"] == baseline["dossier"]["expected_return"]
    assert (
        after["dossier"].get("expected_return_band")
        == baseline["dossier"].get("expected_return_band")
    )
