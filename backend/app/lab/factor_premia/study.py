"""Run the factor-premia study and read back its evidence cards."""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, cast

import pandas as pd

from sqlalchemy.orm import Session

from app.foundation.data_engineering.paths import get_panel_dir
from app.foundation.factor_evidence import (
    TILT_EVIDENCE_REGION,
    factor_evidence_regions,
    latest_factor_evidence_cards,
)
from app.foundation.models.entities import FactorEvidenceCard
from app.foundation.models.entities._core import now_utc
from app.foundation.quant_metrics import record_trial
from app.foundation.settings import get_public_settings
from app.lab.factor_premia.evidence import EvidenceCard, evaluate
from app.lab.factor_premia.portfolios import factor_series
from app.lab.factor_premia.strategies import REGIONS, STRATEGIES

logger = logging.getLogger(__name__)

TRIAL_CONTEXT = "factor_premia"


def _tilt_cap_pct(db: Session) -> float:
    try:
        return float(get_public_settings(db).get("plan_tilt_max_pct") or 15.0)
    except (TypeError, ValueError):
        return 15.0


def run_study(
    db: Session,
    *,
    region: str = TILT_EVIDENCE_REGION,
    panel_dir: Path | None = None,
    persist: bool = True,
) -> list[EvidenceCard]:
    """Compute one evidence card per pre-registered strategy.

    With ``persist`` the cards are stored as one run and each strategy is
    recorded once on the global trial ledger (idempotent per region and
    strategy). Returns an empty list when the panel has no data for the
    region, and then stores nothing.
    """
    if region not in REGIONS:
        raise ValueError(f"unknown region {region!r}; expected one of {sorted(REGIONS)}")
    series = factor_series(panel_dir or get_panel_dir(), REGIONS[region])
    if series.empty:
        logger.warning("factor_premia: no JKP rows for region %s", region)
        return []

    cap = _tilt_cap_pct(db)
    cards = [
        evaluate(s, cast(pd.DataFrame, series[series["strategy"] == s.key]), region=region, tilt_cap_pct=cap)
        for s in STRATEGIES
    ]
    if persist:
        run_id = str(uuid.uuid4())
        computed_at = now_utc()
        for card in cards:
            db.add(FactorEvidenceCard(
                run_id=run_id, strategy=card.strategy, region=region, months=card.months,
                passed=card.passed, card_json=card.as_dict(), computed_at=computed_at,
            ))
            record_trial(
                db, TRIAL_CONTEXT, f"{region}:{card.strategy}",
                {"region": region, "strategy": card.strategy, "pre_registered": True},
            )
        db.commit()
        logger.info(
            "factor_premia: run %s stored %d cards (%d passed)",
            run_id, len(cards), sum(c.passed for c in cards),
        )
    return cards


def latest_cards(db: Session) -> list[dict[str, Any]]:
    """Each region's most recent run, the gating region first, strategies in
    pre-registration order."""
    order = {s.key: i for i, s in enumerate(STRATEGIES)}
    out: list[dict[str, Any]] = []
    for region in factor_evidence_regions(db):
        cards = latest_factor_evidence_cards(db, region)
        out.extend(sorted(cards, key=lambda c: order.get(c.get("strategy", ""), len(order))))
    return out
