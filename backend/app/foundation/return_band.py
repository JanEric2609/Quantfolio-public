"""PRIIPs-style P10/P50/P90 uncertainty band around the discover expected-return anchor.

M5 Option 0 (backend half) of the discover-maths audit fix — audit findings A8/F14:
the dossier's headline number is linear arithmetic on ONE backward-looking figure
presented as forward-looking; the honest companion is a Monte-Carlo dispersion band
over the SAME price series family that feeds the anchor. Blueprint:
docs/archive/audits/2026-08-discover-maths/blueprint_appendix.md M5 Option 0, SUPERSEDED by
Oracle corrections implemented here:

- circular BLOCK bootstrap of daily log returns (not iid, not pure GBM) — block
  resampling preserves the short-term autocorrelation/volatility clustering that
  iid resampling destroys;
- L=21 trading days, B=10000 paths, H=round(months/12*252);
- percentiles are TOTAL returns over the horizon — never annualized;
- log-space anchor centering: every path is shifted by a constant delta so the
  median terminal gross maps exactly onto ``(1+anchor_annual)^(months/12) - 1``
  while dispersion is preserved untouched;
- fewer than ``min_obs`` usable returns => GBM fallback via
  :mod:`app.foundation.quant_mc` calibration + Euler engine (the
  advisor/decision.py:_mc_summary pattern), flagged ``concerns=["wide_band"]``.

The band NEVER changes the point estimate: ``expected_return_pct`` keeps its
pinned linear dampening (clamp ±25, 1dp) — the geometric horizon conversion
appears ONLY inside this band's centering, per the audit decision that the
linear conversion is PINNED for the headline.

Every returned dict carries the house safety tags ``estimate=True`` and
``not_tax_advice=True``. Any internal failure returns ``None`` (fail-open: the
caller omits the band entirely rather than blocking dossier generation).
"""
from __future__ import annotations

import logging
import math

import numpy as np
from sqlalchemy.orm import Session

from app.foundation.market import history as market_history

logger = logging.getLogger(__name__)

# Same series family that feeds the trailing anchor (the backtest window): at
# least ~3y of calendar lookback, or the horizon plus buffer, whichever is longer.
_MIN_HISTORY_DAYS = 365 * 3 + 30


def _band_dict(p10: float, p50: float, p90: float, method: str, n_paths: int, concerns: list[str]) -> dict:
    return {
        "p10": float(p10),
        "p50": float(p50),
        "p90": float(p90),
        "method": method,
        "n_paths": int(n_paths),
        "estimate": True,
        "not_tax_advice": True,
        "concerns": concerns,
    }


def _gbm_fallback_band(prices: np.ndarray, horizon_steps: int, n_paths: int, seed: int) -> dict:
    """Short-history fallback: GBM calibrated via quant_mc, Euler-simulated H steps.

    Imports are lazy to match advisor/decision.py style (quant_mc machinery is
    only needed on this branch).
    """
    from app.foundation.quant_mc.calibration import calibrate_gbm
    from app.foundation.quant_mc.distributions import NormalDistribution
    from app.foundation.quant_mc.paths import GBMEuler

    params = calibrate_gbm(prices)
    sim = GBMEuler(mu=params.mu, sigma=params.sigma)
    spot = float(prices[-1])
    paths = sim.simulate(
        S0=spot,
        T=float(horizon_steps),
        n_steps=horizon_steps,
        n_paths=n_paths,
        rng=np.random.default_rng(seed),
        distribution=NormalDistribution(),
    )
    terminal_returns = paths[:, -1] / spot - 1.0
    p10, p50, p90 = np.percentile(terminal_returns, [10, 50, 90])
    return _band_dict(p10, p50, p90, "gbm_fallback", n_paths, ["wide_band"])


def compute_return_band(
    db: Session,
    symbol: str,
    horizon_months: int,
    anchor_annual: float,
    *,
    min_obs: int = 250,
    n_paths: int = 10000,
    block_length: int = 21,
    seed: int = 42,
    horizon_steps: int | None = None,
    target_total: float | None = None,
) -> dict | None:
    """PRIIPs-style P10/P50/P90 TOTAL-return band over ``horizon_months``.

    Args:
        db: Database session (PriceCache/bar history source).
        symbol: Ticker whose daily closes feed the bootstrap.
        horizon_months: Dossier horizon; H = round(months/12 * 252) trading days.
        anchor_annual: ANNUALIZED return FRACTION (0.08 == 8%) — the same unit
            ``select_return_anchor`` returns. The band median is centered on the
            horizon-converted anchor ``(1+anchor)^(months/12) - 1``.
        min_obs: Minimum daily log returns for the block-bootstrap path; below
            this the GBM fallback runs (flagged ``wide_band``).
        n_paths: Bootstrap path count B.
        block_length: Circular block length L in trading days.
        seed: RNG seed — identical inputs + seed yield byte-identical output.
        horizon_steps: Override H (trading days) — the prediction ledger's own
            horizon is 21 trading days, not a whole number of months.
        target_total: Override the centering TOTAL return over the horizon
            (a fraction) instead of converting ``anchor_annual``; pairs with
            ``horizon_steps`` so a ledger point estimate already stated over its
            own horizon can be banded without a round trip through an annual rate.

    Returns:
        ``{"p10", "p50", "p90", "method", "n_paths", "estimate",
        "not_tax_advice", "concerns"}`` or ``None`` on any failure (fail-open —
        callers omit the band; this function never raises).
    """
    try:
        days = max(_MIN_HISTORY_DAYS, math.ceil(horizon_months / 12 * 365) + 60)
        # allow_live=False: band computation fans out over many symbols inside
        # dossier generation — a synchronous per-symbol provider fetch here
        # would hang the pipeline (see market.history's own docstring). Cached
        # bars/prices only; an empty cache fails open to None below.
        rows = market_history(db, symbol, days=days, allow_live=False)
        dated = sorted(
            (r["date"], float(r["close"])) for r in rows if r.get("close") is not None
        )
        prices = np.array([c for _d, c in dated], dtype=float)

        h_steps = max(1, int(horizon_steps)) if horizon_steps else max(1, round(horizon_months / 12 * 252))

        if len(prices) < 2:
            raise ValueError(f"insufficient price history for {symbol}: {len(prices)} rows")

        if len(prices) - 1 < min_obs:
            return _gbm_fallback_band(prices, h_steps, n_paths, seed)

        log_returns = np.diff(np.log(prices))
        rng = np.random.default_rng(seed)
        n_blocks = math.ceil(h_steps / block_length)
        # Circular block bootstrap: random start per block, wrapping modulo the
        # series length, then trim the flattened blocks to exactly H steps.
        starts = rng.integers(0, len(log_returns), size=(n_paths, n_blocks))
        idx = (starts[:, :, None] + np.arange(block_length)[None, None, :]) % len(log_returns)
        sampled = log_returns[idx.reshape(n_paths, -1)[:, :h_steps]]
        log_gross = sampled.sum(axis=1)
        terminal_gross = np.exp(log_gross)

        if target_total is not None:
            implied_horizon_total_anchor = float(target_total)
        else:
            implied_horizon_total_anchor = (1.0 + anchor_annual) ** (horizon_months / 12.0) - 1.0
        delta = float(np.log1p(implied_horizon_total_anchor) - np.median(np.log(terminal_gross)))
        total_returns = terminal_gross * math.exp(delta) - 1.0

        p10, p50, p90 = np.percentile(total_returns, [10, 50, 90])
        return _band_dict(p10, p50, p90, "block_bootstrap", n_paths, [])
    except Exception as exc:
        logger.warning("compute_return_band failed for %s: %s", symbol, exc)
        return None
