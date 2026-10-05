"""The cross-section AlphaCrafter's miner validates factors on.

A factor's IC is a rank correlation across the assets of one day, so the
miner needs a real cross-section. The AlphaCrafter paper mines on the CSI 300
and the S&P 500 (arXiv 2605.05580). Until 2026-09-28 the default basket was
four world/EM/S&P ETFs (EUNL.DE, VWRL.L, EIMI.L, SXR8.DE). A four-point rank
correlation takes a handful of values, so every nightly run rejected every
factor ("No factors passed IC/ICIR gate") and FactorsLibrary stayed empty.
"""
from __future__ import annotations

import logging
from typing import Any

from app.foundation.index_constituents import EUROSTOXX50, SP100

logger = logging.getLogger(__name__)

# Euro Stoxx 50 + S&P 100: 150 large caps, EUR and USD, the two markets the
# app's DKB account trades.
MINER_UNIVERSE: list[str] = [*EUROSTOXX50, *SP100]

# A configured basket smaller than this is not a cross-section; it is
# replaced by MINER_UNIVERSE rather than mined into guaranteed rejections.
MIN_MINER_UNIVERSE = 30


def resolve_miner_universe(settings: dict[str, Any], universe: list[str] | None = None) -> list[str]:
    """An explicit *universe* as given; else ``alphacrafter_index_basket``
    when it holds at least MIN_MINER_UNIVERSE names; else MINER_UNIVERSE."""
    if universe:
        return list(dict.fromkeys(universe))
    basket = settings.get("alphacrafter_index_basket")
    if isinstance(basket, str):
        candidates = [s.strip() for s in basket.split(",") if s.strip()]
    elif isinstance(basket, list):
        candidates = [str(s).strip() for s in basket if str(s).strip()]
    else:
        candidates = []
    unique = list(dict.fromkeys(candidates))
    if not unique:
        return list(MINER_UNIVERSE)
    if len(unique) < MIN_MINER_UNIVERSE:
        logger.warning(
            "AlphaCrafter: basket of %d symbol(s) is too small for a cross-sectional IC "
            "(need %d); mining MINER_UNIVERSE (%d names) instead",
            len(unique), MIN_MINER_UNIVERSE, len(MINER_UNIVERSE),
        )
        return list(MINER_UNIVERSE)
    return unique
