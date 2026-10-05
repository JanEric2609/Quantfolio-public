"""Canonical instrument-type classifier, shared across Discover, Paper Portfolio,
and Advisor.

Previously ``resolve_instrument_type`` lived in ``discover/pipeline.py`` and was
imported sideways by ``paper_portfolio.py``, ``llm_portfolio/mirror.py``,
``advisor/evolution.py``, and ``advisor/decision.py`` — a Discover-owned module
classifying instruments for four unrelated subsystems. This module is the single
owner instead.

Covers the four classes Quantfolio actually screens or holds: ``equity``,
``etf``, ``bond`` (duration-bearing fixed-income funds, e.g. CBE3.L), and
``money_market`` (cash-equivalent overnight-rate funds, e.g. XEON.DE).
Crypto/real-estate/commodities are out of scope — see
``docs/adr/0001-unified-portfolio-engine.md``. Distinct from ``TargetAllocationItem
.asset_type`` in ``schemas/common.py``, which enumerates goal-planning allocation
buckets, not instrument types.
"""
from __future__ import annotations

import logging
from typing import Literal

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

InstrumentType = Literal["equity", "etf", "money_market", "bond"]


def classify_instrument(symbol: str, source: str | None = None, name: str | None = None) -> InstrumentType:
    """Resolve a candidate/holding to ``"money_market"``, ``"bond"``, ``"etf"``,
    or ``"equity"``.

    ``name`` (when available) is checked first against the overnight-rate /
    money-market keyword list, then the bond keyword list — a cash-equivalent
    instrument (e.g. XEON.DE, tracking EUR €STR) or a duration-bearing bond
    fund (e.g. CBE3.L) must never fall through to the generic ``"etf"``
    bucket, since composite scoring, MC calibration, and the LLM prompt all
    treat that bucket as "diversified-basket-of-equities-shaped". Universe
    source metadata is authoritative for the etf/equity split when present
    (``screen_etf``); otherwise the static ETF table decides (network-free —
    this runs once per candidate inside the pipeline loop). Fails open to
    ``"equity"``.
    """
    if name:
        try:
            from app.foundation.etf_universe import is_bond_fund, is_money_market_fund

            if is_money_market_fund(name):
                return "money_market"
            if is_bond_fund(name):
                return "bond"
        except Exception:
            logger.debug("classify_instrument: money-market/bond check failed for %s", symbol, exc_info=True)
    if source == "screen_etf":
        return "etf"
    try:
        from app.foundation.etf_lookup import _get_static_fallback

        if _get_static_fallback(symbol) is not None:
            return "etf"
    except Exception:
        logger.debug("classify_instrument: lookup failed for %s", symbol, exc_info=True)
    return "equity"


def to_paper_asset_type(instrument_type: InstrumentType) -> str:
    """Map the canonical classifier's output onto ``PaperHolding.asset_type``.

    ``equity`` maps to the legacy ``"stock"`` string; ``etf``/``money_market``/
    ``bond`` pass through unchanged. Kept as one function since it was
    previously duplicated inline in ``paper_portfolio.py`` and
    ``llm_portfolio/mirror.py``.
    """
    return instrument_type if instrument_type in ("etf", "money_market", "bond") else "stock"


def resolve_holding_asset_type(
    db: Session,
    *,
    isin: str | None = None,
    symbol: str | None = None,
    name: str | None = None,
    source: str | None = None,
) -> str:
    """Evidence-first ``asset_type`` for holding-CREATION paths (T3.1, DEC-E).

    Ladder:

    (a) An ``Asset`` row matched by ISIN is authoritative — its
        ``asset_type`` is returned verbatim, even when the symbol/name/source
        heuristics disagree (prod evidence: ``paper_holdings`` drifted to
        stock×12 vs etf×3 under heuristics alone).
    (b) Otherwise the existing :func:`classify_instrument` heuristics decide,
        unchanged: money-market/bond name keywords → ``screen_etf`` source →
        static ETF table → equity fail-open.

    Returns Holding-compatible vocabulary ("stock"/"etf"/"money_market"/
    "bond"): rung (b) goes through :func:`to_paper_asset_type`, so ``equity``
    maps to the legacy ``"stock"`` string, while an Asset row's curated value
    passes through as-is — same contract as
    ``etf_classification.propagate_asset_types`` on the repair side.

    Takes a session but performs NO network calls; the DB read uses
    ``.first()`` so multiple exchange/currency Asset rows sharing an ISIN
    cannot raise, and any lookup failure degrades to rung (b) instead of
    blocking ingestion.
    """
    if isin:
        try:
            from app.foundation.models.entities import Asset

            asset = db.query(Asset).filter(Asset.isin == isin).first()
            if asset is not None and asset.asset_type:
                return asset.asset_type
        except Exception:
            logger.debug("resolve_holding_asset_type: Asset lookup failed for %s", isin, exc_info=True)
    return to_paper_asset_type(classify_instrument(symbol or "", source=source, name=name))
