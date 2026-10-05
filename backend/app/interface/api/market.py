import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.interface.api.safe_endpoint import safe_endpoint
from app.foundation.core.db import get_db
from app.foundation.models.entities import ProviderHealth, User
from app.foundation.schemas import MarketFundamentals, MarketHistoryPoint, MarketQuote
from app.foundation import market as market_service
from app.foundation.providers.registry import build_provider_registry
from app.foundation.auth import current_user

router = APIRouter(prefix="/api/market", tags=["market"])


@router.get("/quote/{ticker}", response_model=MarketQuote)
@safe_endpoint
def quote(ticker: str, db: Session = Depends(get_db), _user: User = Depends(current_user)) -> dict[str, Any]:
    return market_service.quote(db, ticker)


@router.get("/history/{ticker}", response_model=list[MarketHistoryPoint])
@safe_endpoint
def history(
    ticker: str,
    days: int = 365,
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> list[dict[str, Any]]:
    return market_service.history(db, ticker, days)


@router.get("/fundamentals/{ticker}", response_model=MarketFundamentals)
@safe_endpoint
def fundamentals(ticker: str, db: Session = Depends(get_db), _user: User = Depends(current_user)) -> dict[str, Any]:
    return market_service.fundamentals(db, ticker)


@router.get("/providers/status")
@safe_endpoint
def providers_status(db: Session = Depends(get_db), _user: User = Depends(current_user)) -> dict[str, Any]:
    return {"providers": market_service.provider_status(db)}


@router.get("/providers/health")
@safe_endpoint
def providers_health(db: Session = Depends(get_db), _user: User = Depends(current_user)) -> dict[str, Any]:
    statuses = market_service.provider_status(db)
    rows = [_upsert_health(db, status, "status") for status in statuses]
    db.commit()
    return {"providers": [_health_to_dict(row) for row in rows]}


@router.post("/providers/{provider}/test")
@safe_endpoint
def provider_test(provider: str, db: Session = Depends(get_db), _user: User = Depends(current_user)) -> dict[str, Any]:
    registry = build_provider_registry(db)
    matched = next((item for item in registry.providers if item.name == provider), None)
    if matched is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    result = matched.get_quote("EUNL.DE")
    status_payload = matched.status()
    ok = bool(result.get("ok"))
    status_payload["available"] = ok
    status_payload["message"] = (
        f"Quote test succeeded for EUNL.DE via {provider}."
        if ok
        else "; ".join(result.get("quality", {}).get("warnings", [])) or result.get("error") or "Provider test failed."
    )
    row = _upsert_health(db, status_payload, "quote")
    row.last_success_at = datetime.now(UTC) if ok else row.last_success_at
    row.last_error_at = None if ok else datetime.now(UTC)
    row.meta_json = json.dumps({"quality": result.get("quality", {}), "error": result.get("error")}, default=str)
    db.commit()
    db.refresh(row)
    return _health_to_dict(row)


def _upsert_health(db: Session, status_payload: dict[str, Any], capability: str) -> ProviderHealth:
    provider = status_payload.get("provider", "unknown")
    row = (
        db.query(ProviderHealth)
        .filter(ProviderHealth.provider == provider, ProviderHealth.capability == capability)
        .one_or_none()
    )
    if row is None:
        row = ProviderHealth(provider=provider, capability=capability, message="")
        db.add(row)
    enabled = bool(status_payload.get("enabled", status_payload.get("available", False)))
    available = bool(status_payload.get("available", False))
    row.configured = enabled
    row.available = available
    row.status = "healthy" if available else "missing" if not enabled else "degraded"
    row.message = str(status_payload.get("message") or "")
    row.updated_at = datetime.now(UTC)
    return row


def _health_to_dict(row: ProviderHealth) -> dict[str, Any]:
    return {
        "id": row.id,
        "provider": row.provider,
        "capability": row.capability,
        "configured": row.configured,
        "available": row.available,
        "status": row.status,
        "message": row.message,
        "last_success_at": row.last_success_at,
        "last_error_at": row.last_error_at,
        "meta": json.loads(row.meta_json or "{}"),
        "updated_at": row.updated_at,
    }
