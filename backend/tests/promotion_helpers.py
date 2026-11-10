"""Seed matched shadow-forecast history for the paired promotion test (ADR 0025)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import numpy as np

from app.foundation.models.entities import AdvisorShadowForecast, PaperSnapshot


def seed_paired_history(
    db,
    user_id: str,
    champion,
    challenger,
    *,
    challenger_edge: float,
    days: int = 100,
    tickers: int = 3,
    spawn_days_ago: int = 160,
    seed: int = 0,
    nav_gap: float = 0.0,
) -> date:
    """Resolved shadow forecasts for both strategies on the same (date, ticker).

    The champion says p = 0.5 plus noise; the challenger leans
    ``challenger_edge`` toward the realised outcome (negative = away from it).
    Both sleeves get the same NAV walk (``nav_gap`` adds a daily drift to the
    challenger's). Returns the first forecast date.
    """
    rng = np.random.default_rng(seed)
    today = datetime.now(UTC).date()
    born = today - timedelta(days=spawn_days_ago)
    challenger.created_at = datetime.combine(born, datetime.min.time(), UTC)
    champion.created_at = datetime.combine(born - timedelta(days=30), datetime.min.time(), UTC)
    first = np.busday_offset(born, 1, roll="forward").astype(date)
    day = first
    resolved = datetime.now(UTC)
    for _ in range(days):
        for k in range(tickers):
            beat = bool(rng.random() < 0.5)
            outcome = 1.0 if beat else 0.0
            p_champ = float(np.clip(0.5 + rng.normal(0, 0.05), 0.01, 0.99))
            lean = challenger_edge if beat else -challenger_edge
            p_chall = float(np.clip(0.5 + lean + rng.normal(0, 0.05), 0.01, 0.99))
            for strategy, p in ((champion, p_champ), (challenger, p_chall)):
                db.add(AdvisorShadowForecast(
                    id=uuid4().hex, user_id=user_id, strategy_id=strategy.id, forecast_date=day,
                    ticker=f"T{k}", horizon_days=21, p_beat=p, band_low=-0.1, band_high=0.1,
                    resolved_at=resolved, beat=beat, brier=(p - outcome) ** 2, rps=0.05,
                    realised_return=0.01,
                ))
        day = np.busday_offset(day, 1, roll="forward").astype(date)
    nav = 100_000.0
    d = born
    while d < today:
        if d.weekday() < 5:
            nav *= 1.0 + rng.normal(0.0003, 0.008)
            for strategy, extra in ((champion, 0.0), (challenger, nav_gap)):
                value = nav * (1.0 + extra) ** ((d - born).days)
                db.add(PaperSnapshot(
                    id=uuid4().hex, portfolio_id=strategy.portfolio_id, date=d,
                    total_value=Decimal(str(round(value, 2))),
                ))
        d += timedelta(days=1)
    db.commit()
    return first
