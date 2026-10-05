"""Listing-suffix facts shared by the Discover pipeline and outcome resolution."""
from __future__ import annotations

# The suffix rule lives in foundation next to listing_region; a stored
# provider-reported currency beats it (data_backbone.listing_currency).
from app.foundation.providers.utils import listing_currency

__all__ = ["is_eu_listing", "listing_currency"]

_EU_SUFFIXES = (
    ".DE", ".F", ".BE", ".MU", ".STU", ".HM", ".DU", ".SG",  # Germany
    ".PA", ".AS", ".BR", ".LS", ".MC", ".MI", ".VI", ".IR",  # Euronext / South EU
    ".HE", ".ST", ".CO", ".OL", ".IC",                          # Nordics
    ".SW", ".VX", ".L",                                          # CH / UK
)


def is_eu_listing(symbol: str) -> bool:
    """True when the symbol carries a European venue suffix."""
    s = symbol.upper()
    return any(s.endswith(suf) for suf in _EU_SUFFIXES)
