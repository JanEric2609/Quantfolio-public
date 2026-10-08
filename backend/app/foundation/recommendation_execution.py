"""Accepted recommendations, the broker links to act on them, and the trade the next sync finds.

The app never orders anything. Accepting a recommendation records the
decision and the position held at each broker at that moment; the person
places the order themselves through a link to the broker. After each DKB or
Scalable sync, :func:`detect_executions` compares the synced positions with
that baseline: more units of a buy idea (fewer of a sell) at a broker means the
trade was done there, and the recommendation moves to ``executed`` with a
review row saying where and how much. Nothing is logged by hand.

Positions, not transactions, are compared because DKB's FinTS sync reports
depot holdings but no depot trades; Scalable's would allow either.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.live_positions import BROKER_LABELS, live_positions
from app.foundation.models.entities import Recommendation, RecommendationReview

logger = logging.getLogger(__name__)

#: An accepted recommendation is watched for this long, then left alone.
WATCH_DAYS = 60
SELL_VERDICTS = {"SELL", "REDUCE", "TRIM", "EXIT", "AVOID"}

_DKB_SEARCH_URL = "https://www.dkb.de/privatkunden/investieren/wertpapiersuche"
_SCALABLE_SECURITY_URL = "https://de.scalable.capital/broker/security"


def dkb_search_url(isin: str | None) -> str:
    """DKB's public securities search, opened on *isin* when there is one.

    The old ``/banking/wertpapiersuche?q=<ticker>`` address answers 404, and
    a ticker such as "C" or "MU" is not something DKB's search resolves
    anyway (checked 2026-09-28). Without an ISIN it opens empty and the user
    searches by name or WKN.
    """
    if isin and len(isin.strip()) == 12:
        return f"{_DKB_SEARCH_URL}?isin={isin.strip().upper()}"
    return _DKB_SEARCH_URL


def scalable_security_url(isin: str | None) -> str | None:
    """Scalable's security page for *isin* (after login; checked 2026-10-05). ``None`` without an ISIN."""
    if isin and len(isin.strip()) == 12:
        return f"{_SCALABLE_SECURITY_URL}?isin={isin.strip().upper()}"
    return None


def broker_links(isin: str | None) -> list[dict[str, str]]:
    """``[{broker, label, url}]`` for every broker with a usable link."""
    out = [{"broker": "dkb", "label": "DKB", "url": dkb_search_url(isin)}]
    scalable = scalable_security_url(isin)
    if scalable:
        out.append({"broker": "scalable", "label": "Scalable", "url": scalable})
    return out


def _payload(rec: Recommendation) -> dict[str, Any]:
    try:
        value = json.loads(rec.payload_json or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def recommendation_isin(rec: Recommendation) -> str | None:
    payload = _payload(rec)
    candidate = payload.get("candidate")
    isin = (candidate if isinstance(candidate, dict) else {}).get("isin") or payload.get("isin")
    return str(isin).upper() if isinstance(isin, str) and len(isin.strip()) == 12 else None


def side(rec: Recommendation) -> str:
    return "sell" if (rec.verdict or "").upper() in SELL_VERDICTS else "buy"


def _quantities(db: Session, user_id: str, isin: str | None, ticker: str | None) -> dict[str, Decimal]:
    """Units held per broker source for the security (ISIN first, ticker when there is none)."""
    out: dict[str, Decimal] = {}
    for pos in live_positions(db, user_id):
        same = (isin and pos.isin and pos.isin.upper() == isin) or (
            not isin and ticker and pos.ticker and pos.ticker.upper() == ticker.upper()
        )
        if same:
            out[pos.source] = out.get(pos.source, Decimal(0)) + Decimal(pos.quantity or 0)
    return out


def record_acceptance(db: Session, rec: Recommendation, *, now: datetime | None = None) -> dict[str, Any]:
    """Store the per-broker position at acceptance in the recommendation's payload (no commit)."""
    now = now or datetime.now(UTC)
    payload = _payload(rec)
    baseline = _quantities(db, rec.user_id, recommendation_isin(rec), rec.ticker)
    payload["execution"] = {
        "accepted_at": now.isoformat(),
        "side": side(rec),
        "baseline": {k: str(v) for k, v in baseline.items()},
    }
    rec.payload_json = json.dumps(payload)
    return payload["execution"]


def execution_info(rec: Recommendation) -> dict[str, Any] | None:
    info = _payload(rec).get("execution")
    return info if isinstance(info, dict) else None


def detect_executions(db: Session, user_id: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Mark accepted recommendations whose trade shows up in the synced positions; returns what changed."""
    now = now or datetime.now(UTC)
    rows = (
        db.query(Recommendation)
        .filter(Recommendation.user_id == user_id, Recommendation.approval_state == "accepted")
        .all()
    )
    found: list[dict[str, Any]] = []
    for rec in rows:
        info = execution_info(rec)
        if not info or not info.get("accepted_at"):
            continue
        try:
            accepted_at = datetime.fromisoformat(info["accepted_at"])
        except ValueError:
            continue
        if accepted_at.tzinfo is None:
            accepted_at = accepted_at.replace(tzinfo=UTC)
        if now - accepted_at > timedelta(days=WATCH_DAYS):
            continue
        baseline = {k: Decimal(str(v)) for k, v in (info.get("baseline") or {}).items()}
        current = _quantities(db, user_id, recommendation_isin(rec), rec.ticker)
        direction = 1 if info.get("side", "buy") == "buy" else -1
        for source in sorted(set(baseline) | set(current)):
            delta = current.get(source, Decimal(0)) - baseline.get(source, Decimal(0))
            if delta * direction <= 0:
                continue
            label = BROKER_LABELS.get(source, source.title())
            note = f"Detected after the {label} sync: {'+' if delta > 0 else ''}{delta.normalize():f} units."
            plan_action_id = _fulfil_plan_action(
                db, user_id, recommendation_isin(rec), rec.ticker, note,
                broker=source, sell=direction < 0, now=now,
            )
            info.update({"executed_at": now.isoformat(), "broker": source, "units": str(delta)})
            if plan_action_id is not None:
                info["plan_action_id"] = plan_action_id
            payload = _payload(rec)
            payload["execution"] = info
            rec.payload_json = json.dumps(payload)
            db.add(RecommendationReview(
                recommendation_id=rec.id, user_id=user_id, from_state="accepted", to_state="executed", notes=note,
            ))
            rec.approval_state = "executed"
            found.append({"id": rec.id, "ticker": rec.ticker, "broker": source, "units": float(delta)})
            break
    if found:
        db.commit()
    return found


def _fulfil_plan_action(
    db: Session, user_id: str, isin: str | None, ticker: str | None,
    note: str, *, broker: str, sell: bool, now: datetime,
) -> str | None:
    """Mark the newest open plan action for this trade fulfilled (ADR 0019 §3).

    Same user, same ISIN (or ticker when there is none), same direction (a sell
    only fulfils a ``sale``, a buy anything else) and a compatible broker (the
    one that moved, or an action with no fixed broker). Queries the
    foundation-tier ``MonthlyPlanAction`` entity directly, so this module
    grows no edge into the decision layer that owns the snapshot writer.
    """
    from app.foundation.models.entities import MonthlyPlanAction

    query = db.query(MonthlyPlanAction).filter(
        MonthlyPlanAction.user_id == user_id,
        MonthlyPlanAction.status == "open",
    )
    if isin:
        query = query.filter(MonthlyPlanAction.isin == isin.upper())
    elif ticker:
        query = query.filter(MonthlyPlanAction.ticker == ticker.upper())
    else:
        return None
    query = query.filter(
        (MonthlyPlanAction.kind == "sale") if sell else (MonthlyPlanAction.kind != "sale"),
        MonthlyPlanAction.broker.in_((broker, "", "auto")),
    )
    action = query.order_by(MonthlyPlanAction.month.desc()).first()
    if action is None:
        return None
    action.status = "fulfilled"
    action.fulfilled_at = now
    detail = dict(action.detail_json or {})
    detail["fulfilled_note"] = note
    action.detail_json = detail
    return action.id


def accepted_and_executed(db: Session, user_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """The Decide page's "after Accept" list: still waiting for the trade, and recently done."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=WATCH_DAYS)
    rows = (
        db.query(Recommendation)
        .filter(
            Recommendation.user_id == user_id,
            Recommendation.approval_state.in_(("accepted", "executed")),
            Recommendation.created_at >= cutoff,
        )
        .order_by(Recommendation.created_at.desc())
        .all()
    )
    waiting, done = [], []
    for rec in rows:
        info = execution_info(rec) or {}
        candidate = _payload(rec).get("candidate")
        isin = recommendation_isin(rec)
        item = {
            "id": rec.id,
            "ticker": rec.ticker,
            "name": (candidate if isinstance(candidate, dict) else {}).get("name"),
            "isin": isin,
            "side": info.get("side") or side(rec),
            "accepted_at": info.get("accepted_at"),
            "links": broker_links(isin),
        }
        if rec.approval_state == "executed":
            item.update({
                "executed_at": info.get("executed_at"),
                "broker": info.get("broker"),
                "broker_label": BROKER_LABELS.get(str(info.get("broker")), str(info.get("broker") or "").title()),
                "units": float(info["units"]) if info.get("units") else None,
                "plan_action_id": info.get("plan_action_id"),
            })
            done.append(item)
        else:
            waiting.append(item)
    return {"waiting": waiting, "executed": done}
