"""Loss-bucket classification (Aktien vs. Sonstige) per §20 EStG."""
from __future__ import annotations


def classify_loss_bucket(asset_type: str | None, fund_class: str | None = None) -> str:
    """Return ``"aktien"`` for single-stock losses, ``"sonstige"`` otherwise.

    Per §20 (6) EStG, losses from direct equity holdings can only offset
    equity gains. Losses from funds/ETFs/bonds fall into the general bucket.
    """
    asset = (asset_type or "").lower()
    if asset in {"stock", "equity", "share", "aktie"}:
        return "aktien"
    return "sonstige"
