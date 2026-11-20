"""M5 Option 2: Black-Litterman posterior anchor behind er_mode="bl".

Audit F14 + blueprint M5 Option 2, SUPERSEDED by Oracle corrections implemented
here: proportional-Omega construction (NEVER Idzorek — naming banned), Omega =
k·τ·PΣPᵀ with a SINGLE shared k so the posterior mean reduces to the closed
form  μ_post = Π + (1/(1+k))·ΣPᵀ(PΣPᵀ)⁻¹(Q − P·Π), which is EXACTLY
tau-invariant (the per-view-k diagonal breaks exact cancellation, hence the
shared-k pin); ONE tau source (BL_TAU_DEFAULT) shared by blm.py and
quant_optim.py to kill the 0.025-vs-0.05 drift; collinear views guarded the
module's way (BLMError).
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
from app.decision.discover import blm
from app.decision.discover.dossier_writer import _llm_view, write_dossier
from app.decision.discover.blm import (
    BL_TAU_DEFAULT,
    BLMError,
    blm_posterior_proportional,
    compute_equilibrium_returns,
)
from app.foundation.settings import upsert_public_settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_prices(db, ticker: str, closes: list[float]) -> None:
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


def _aligned_pair_closes(n: int = 420, seed: int = 13) -> tuple[list[float], list[float]]:
    """Two correlated business-day close series (same calendar, different drifts)."""
    rng = np.random.default_rng(seed)
    steps_a = [rng.normal(0.003, 0.012) for _ in range(n)]
    steps_b = [rng.normal(0.001, 0.008) for _ in range(n)]
    return (
        list(100.0 * np.cumprod(np.exp(steps_a))),
        list(80.0 * np.cumprod(np.exp(steps_b))),
    )


_SIGMA = np.array([[0.04, 0.01], [0.01, 0.09]])
_W_MKT = np.array([0.5, 0.5])


def _candidate(symbol: str, scores: dict) -> dict:
    return {
        "symbol": symbol,
        "name": "Test Instrument",
        "isin": "US0378331005",
        "source": "screen_index",
        "scores_json": json.dumps(scores),
        "tradeable_json": json.dumps({"teilfreistellung_class": "aktien", "domicile": "US"}),
    }


def _mock_llm(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._local_llm_sync",
        lambda _db, _messages, timeout_s=120.0, **_kw: json.dumps(
            {"direction": "long", "conviction": 0.8, "horizon_months": 6,
             "thesis": "T.", "key_risks": []}
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


_SCORES = {
    "composite": 0.8,
    "quant_signals": {"instrument_type": "equity"},
    "backtest_vs_benchmark": {"candidate_return_annual": 0.20},
}


# ---------------------------------------------------------------------------
# Pure BL math
# ---------------------------------------------------------------------------


def test_equilibrium_prior_equals_lambda_sigma_w_mkt():
    """Π = δ·Σ·w_mkt reproduced manually within 1e-9."""
    pi = compute_equilibrium_returns(_SIGMA, _W_MKT, risk_aversion=2.5)

    manual = 2.5 * (_SIGMA @ _W_MKT)
    assert np.allclose(pi, manual, atol=1e-9)


def test_posterior_tau_invariant_across_001_to_010():
    """Shared-k proportional Omega cancels tau EXACTLY: sweep [0.01, 0.05, 0.10]
    → identical posterior means (atol 1e-8)."""
    pi = compute_equilibrium_returns(_SIGMA, _W_MKT, risk_aversion=2.5)
    views = {0: 0.15}
    confidences = {0: 0.9}

    means = [
        blm_posterior_proportional(pi, _SIGMA, views, confidences, tau=t)[0]
        for t in (0.01, 0.05, 0.10)
    ]

    for other in means[1:]:
        assert np.allclose(means[0], other, atol=1e-8)


def test_confidence_scales_tilt_monotonically():
    """Higher confidence (lower k) pulls the posterior toward the view; lower
    confidence stays nearer the prior."""
    pi = compute_equilibrium_returns(_SIGMA, _W_MKT, risk_aversion=2.5)
    view_q = 0.20

    confident = blm_posterior_proportional(pi, _SIGMA, {0: view_q}, {0: 0.99})[0]
    timid = blm_posterior_proportional(pi, _SIGMA, {0: view_q}, {0: 0.10})[0]

    assert confident[0] > timid[0] > pi[0]
    assert abs(confident[0] - view_q) < abs(timid[0] - view_q)


def test_collinear_views_guarded():
    """A near-singular PΣPᵀ (duplicated asset ⇒ rank-deficient Σ) raises
    BLMError per module convention — no crash, no NaNs leaking out."""
    dup_sigma = np.full((2, 2), 0.04)
    pi = compute_equilibrium_returns(dup_sigma, _W_MKT, risk_aversion=2.5)

    with pytest.raises(BLMError):
        blm_posterior_proportional(pi, dup_sigma, {0: 0.1, 1: 0.2}, {0: 0.9, 1: 0.9})

    # Empty views degrade to the prior unchanged (mirrors blm_posterior).
    same, cov = blm_posterior_proportional(pi, _SIGMA, {}, {})
    assert np.array_equal(same, np.asarray(pi))
    assert np.array_equal(cov, _SIGMA)


def test_views_plumb_through_blm_types(monkeypatch):
    """Dispatcher path must call blm.compute_equilibrium_returns and
    blm_posterior_proportional — no parallel BL math outside blm.py."""
    db = _memory_db()
    cand_closes, bench_closes = _aligned_pair_closes()
    _seed_prices(db, "PLUMB", cand_closes)
    _seed_prices(db, "SPY", bench_closes)
    _mock_llm(monkeypatch)
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._llm_view", lambda _db, _symbol: (0.5, 0.04)
    )

    calls = {"eq": 0, "post": 0}
    real_eq = blm.compute_equilibrium_returns
    real_post = blm.blm_posterior_proportional

    def _spy_eq(*args, **kwargs):
        calls["eq"] += 1
        return real_eq(*args, **kwargs)

    def _spy_post(*args, **kwargs):
        calls["post"] += 1
        return real_post(*args, **kwargs)

    monkeypatch.setattr(blm, "compute_equilibrium_returns", _spy_eq)
    monkeypatch.setattr(blm, "blm_posterior_proportional", _spy_post)

    upsert_public_settings(db, {"er_mode": "bl"})
    result = write_dossier(db, _candidate("PLUMB", _SCORES), {"user_id": "user1"})

    assert calls["eq"] >= 1 and calls["post"] >= 1
    assert result["dossier"]["expected_return_anchor_kind"] == "bl_posterior"


# ---------------------------------------------------------------------------
# LLM view seam
# ---------------------------------------------------------------------------


def test_llm_view_seam_sentinel_and_success(monkeypatch):
    """All-queries-failed (blm.multi_query_confidence returns None, ADR 0014
    §5) maps to None; a usable reading passes through untouched."""

    async def _all_failed(*_args, **_kwargs):
        return None

    async def _usable(*_args, **_kwargs):
        return 0.42, 0.03

    db = _memory_db()
    monkeypatch.setattr(blm, "multi_query_confidence", _all_failed)
    assert _llm_view(db, "NOVIEW") is None

    monkeypatch.setattr(blm, "multi_query_confidence", _usable)
    assert _llm_view(db, "VIEW") == (0.42, 0.03)


# ---------------------------------------------------------------------------
# Dispatcher integration + rollback contract (S-M5b extension)
# ---------------------------------------------------------------------------


def test_er_mode_bl_flips_and_degrades_cleanly(monkeypatch):
    """er_mode="bl" without a usable LLM view is byte-identical to trailing;
    with a confident positive view it flips to bl_posterior and differs;
    flipping back to "trailing" restores the baseline byte-identically."""
    db = _memory_db()
    cand_closes, bench_closes = _aligned_pair_closes()
    _seed_prices(db, "BLC", cand_closes)
    _seed_prices(db, "SPY", bench_closes)
    _mock_llm(monkeypatch)

    baseline = write_dossier(db, _candidate("BLC", _SCORES), {"user_id": "user1"})
    assert baseline["dossier"]["expected_return_anchor_kind"] == "trailing_3y_annualized_return"

    upsert_public_settings(db, {"er_mode": "bl"})
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._llm_view", lambda _db, _symbol: None
    )
    degraded = write_dossier(db, _candidate("BLC", _SCORES), {"user_id": "user1"})
    assert degraded["dossier"] == baseline["dossier"]

    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._llm_view", lambda _db, _symbol: (0.6, 0.02)
    )
    flipped = write_dossier(db, _candidate("BLC", _SCORES), {"user_id": "user1"})
    f_dossier = flipped["dossier"]
    assert f_dossier["expected_return_anchor_kind"] == "bl_posterior"
    assert f_dossier["expected_return"] != baseline["dossier"]["expected_return"]
    assert f_dossier.get("expected_return_band") is not None

    upsert_public_settings(db, {"er_mode": "trailing"})
    rolled_back = write_dossier(db, _candidate("BLC", _SCORES), {"user_id": "user1"})
    assert rolled_back["dossier"] == baseline["dossier"]


def test_tau_drift_unified():
    """THE single-tau-source pin: quant_optim's default IS blm's constant —
    the historical 0.025-vs-0.05 drift can never reopen."""
    with pytest.raises(ImportError):
        # The duplicate quant_optim Black-Litterman surface is deleted
        # (ADR 0003 decision 3); only blm_posterior may define a tau default.
        from app.foundation.quant_optim import black_litterman_optimize  # noqa: F401

    posterior_tau = inspect.signature(blm.blm_posterior).parameters["tau"].default

    assert posterior_tau == BL_TAU_DEFAULT
    assert BL_TAU_DEFAULT == 0.025
