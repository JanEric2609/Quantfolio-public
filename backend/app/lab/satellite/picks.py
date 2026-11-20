"""Which stocks the satellite holds: the pre-registered buy and hold rules.

At each review month the satellite keeps a holding while the stock still has
a score and it is at least the eligible stocks' ``1 - HOLD_WHILE_TOP``
quantile, and fills the free slots with the best-scored eligible stocks it
does not hold. Ties break by ``gvkey`` so a run is reproducible.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import pandas as pd

from app.lab.satellite.spec import ELIGIBLE_SIZE_GROUPS


@dataclass(frozen=True)
class Review:
    """The trades of one review month; months without trades are left out."""

    eom: pd.Timestamp
    sells: tuple[str, ...]
    buys: tuple[str, ...]


def pick(
    rows: pd.DataFrame,
    score: str,
    thresholds: pd.Series,
    review_months: list[pd.Timestamp],
    positions: int,
) -> list[Review]:
    """Walk the review months in order and return the trades.

    ``rows`` has ``eom``, ``gvkey``, ``size_grp`` and the score column, for
    every stock that could be bought or is held. ``thresholds`` is the hold
    bar per review month, indexed by ``eom``.
    """
    by_month = {eom: frame for eom, frame in rows.groupby("eom", sort=False)}
    held: list[str] = []
    reviews: list[Review] = []
    for eom in review_months:
        month = by_month.get(eom)
        if month is None:
            month = rows.iloc[0:0]
        scored = cast(pd.DataFrame, month[month[score].notna()])
        bar = thresholds.get(eom)
        now = dict(zip(scored["gvkey"], scored[score], strict=True))
        keep = [g for g in held if bar is not None and g in now and now[g] >= bar]
        pool = cast(pd.DataFrame, scored[scored["size_grp"].isin(ELIGIBLE_SIZE_GROUPS) & ~scored["gvkey"].isin(keep)])
        pool = pool.sort_values([score, "gvkey"], ascending=[False, True])
        buys = [str(g) for g in pool["gvkey"].iloc[: positions - len(keep)]]
        sells = [g for g in held if g not in keep]
        if sells or buys:
            reviews.append(Review(eom, tuple(sells), tuple(buys)))
        held = keep + buys
    return reviews


def holding_months(reviews: list[Review], months: list[pd.Timestamp]) -> list[int]:
    """How long each position was held, in months (open ones to the end)."""
    index = {m: i for i, m in enumerate(months)}
    opened: dict[str, int] = {}
    spans: list[int] = []
    for review in reviews:
        at = index[review.eom]
        for g in review.sells:
            spans.append(at - opened.pop(g))
        for g in review.buys:
            opened[g] = at
    spans.extend(len(months) - at for at in opened.values())
    return spans
