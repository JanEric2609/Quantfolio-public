import json
import logging
from decimal import Decimal
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.foundation.core.db import get_db
from app.foundation.models.entities import (
    DkbAccount,
    DkbDiagnosticRun,
    DkbPosition,
    DkbSyncLog,
    DkbTransaction,
    User,
)
from app.foundation.schemas import DkbProviderStatus, DkbSyncResponse
from app.foundation.auth import current_user
from app.foundation.dkb import (
    DKB_PUBLIC_TEST_PRODUCT_ID,
    DkbFinTSAdapter,
    DkbFinTSConnectionRejected,
    DkbFinTSManualTanRequired,
    DkbFinTSTanTimeout,
    _safe_fints_url,
    _sanitize_fints_error,
    dkb_sync_service,
)
from app.foundation.notify import create_notification
from app.foundation.settings import get_public_settings, get_secret

_api_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dkb", tags=["dkb"])


@router.post("/sync", response_model=DkbSyncResponse)
def sync_dkb(
    force: bool = False,
    use_test_product_id: bool = False,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> DkbSyncResponse:
    session = dkb_sync_service.trigger(db, user.id, force=force, use_test_product_id=use_test_product_id)
    return _sync_response(session)


@router.get("/sync/status", response_model=DkbSyncResponse)
def sync_status(session_id: str, user: User = Depends(current_user)) -> DkbSyncResponse:
    session = dkb_sync_service.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown DKB sync session")
    return _sync_response(session)


def _sync_response(session) -> DkbSyncResponse:
    return DkbSyncResponse(
        session_id=session.session_id,
        state=session.state,
        message=session.message,
        challenge=session.challenge,
        challenge_html=session.challenge_html,
        decoupled=session.decoupled,
        available_tan_methods=session.available_tan_methods,
        next_poll_after_seconds=session.next_poll_after_seconds,
        created_at=session.created_at,
        updated_at=session.updated_at,
        provider=session.provider,
        logs=session.logs,
    )


_FINTS_DEFAULT_URL = "https://fints.dkb.de/fints"


@router.get("/provider/status", response_model=DkbProviderStatus)
def provider_status(db: Session = Depends(get_db), user: User = Depends(current_user)) -> DkbProviderStatus:
    public = get_public_settings(db)
    secret = get_secret(db, "dkb")
    meta = secret[1] if secret else {}
    product_id = public.get("dkb_product_id") or meta.get("product_id")
    product_id_source = "AppSetting" if public.get("dkb_product_id") else ("meta" if meta.get("product_id") else None)
    product_id_preview: str | None = None
    if product_id:
        pid = str(product_id)
        product_id_preview = f"{pid[:4]}…{pid[-4:]} ({len(pid)} chars)" if len(pid) > 8 else pid
    configured = bool(secret and meta.get("username"))
    fints_url = public.get("dkb_fints_url") or _FINTS_DEFAULT_URL
    if not configured:
        message = "DKB credentials are missing. Save username and PIN in Settings."
    elif not product_id:
        message = f"FinTS is ready -- no product ID configured, built-in default will be used. Endpoint: {fints_url}"
    else:
        message = f"FinTS is ready for a manual read-only sync. Endpoint: {fints_url}"
    return DkbProviderStatus(
        provider="fints",
        configured=configured,
        product_id_required=False,
        product_id_configured=bool(product_id),
        product_id_preview=product_id_preview,
        product_id_source=product_id_source,
        message=message,
    )


@router.post("/diagnostics")
def run_diagnostics(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    import importlib.util

    public = get_public_settings(db)
    secret = get_secret(db, "dkb")
    meta = secret[1] if secret else {}
    product_id = public.get("dkb_product_id") or meta.get("product_id")
    username = meta.get("username")
    fints_available = importlib.util.find_spec("fints") is not None
    fints_url = public.get("dkb_fints_url") or _FINTS_DEFAULT_URL
    steps: list[dict[str, Any]] = []

    def step(key: str, label: str, status: str, message: str) -> None:
        steps.append({"key": key, "label": label, "status": status, "message": message})

    step("provider", "Provider", "passed", "FinTS (python-fints) is the only DKB adapter.")
    step(
        "credentials",
        "Credentials stored",
        "passed" if secret and username else "failed",
        "Encrypted DKB secret and username are present." if secret and username else "Save DKB username and PIN first.",
    )
    step(
        "product_id",
        "FinTS product ID",
        "passed",
        "Product ID is configured." if product_id else "No product ID configured -- built-in default will be used (DKB does not require registration).",
    )
    step(
        "package",
        "python-fints package",
        "passed" if fints_available else "failed",
        "python-fints is installed." if fints_available else "python-fints is not installed in the backend environment.",
    )
    step(
        "endpoint",
        "FinTS endpoint",
        "passed",
        f"Endpoint: {fints_url}",
    )
    step(
        "tan",
        "TAN method",
        "passed" if public.get("dkb_tan_security_function") or public.get("dkb_tan_medium") else "warning",
        "TAN preference saved." if public.get("dkb_tan_security_function") or public.get("dkb_tan_medium") else "No TAN method pinned; adapter will auto-discover DKB-App (decoupled).",
    )
    latest_log = (
        db.query(DkbSyncLog)
        .filter(
            DkbSyncLog.user_id == user.id,
            ~DkbSyncLog.message.startswith("FinTS exchange:"),
        )
        .order_by(DkbSyncLog.created_at.desc())
        .first()
    )
    step(
        "last_sync",
        "Last sync signal",
        "passed" if latest_log and latest_log.state in {"confirmed", "cached"} else "warning" if latest_log else "warning",
        f"{latest_log.state}: {latest_log.message}" if latest_log else "No DKB sync has been recorded yet.",
    )
    failed = [item for item in steps if item["status"] == "failed"]
    warnings = [item for item in steps if item["status"] == "warning"]
    status = "failed" if failed else "warning" if warnings else "passed"
    summary = (
        f"{len(failed)} blocking DKB configuration issue(s) found."
        if failed
        else f"{len(warnings)} DKB warning(s); a manual read-only sync can be tried."
        if warnings
        else "DKB diagnostics passed; manual read-only sync is ready."
    )
    row = DkbDiagnosticRun(
        user_id=user.id,
        provider="fints",
        status=status,
        summary=summary,
        steps_json=json.dumps(steps),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(row)
    db.flush()
    if status == "failed":
        create_notification(
            db,
            user.id,
            source="dkb",
            title="DKB connection needs attention",
            body=summary,
            severity="critical",
            href="/settings/dkb",
        )
    db.commit()
    db.refresh(row)
    return _diagnostic_to_dict(row)


@router.get("/diagnostics")
def diagnostics(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[dict[str, Any]]:
    rows = (
        db.query(DkbDiagnosticRun)
        .filter(DkbDiagnosticRun.user_id == user.id)
        .order_by(DkbDiagnosticRun.created_at.desc())
        .limit(20)
        .all()
    )
    return [_diagnostic_to_dict(row) for row in rows]


@router.get("/sync/logs", response_model=None)
def sync_logs(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[DkbSyncLog]:
    return (
        db.query(DkbSyncLog)
        .filter(DkbSyncLog.user_id == user.id)
        .order_by(DkbSyncLog.created_at.desc())
        .limit(100)
        .all()
    )


@router.get("/accounts", response_model=None)
def accounts(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[DkbAccount]:
    return db.query(DkbAccount).filter(DkbAccount.user_id == user.id).all()


@router.get("/transactions", response_model=None)
def transactions(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[DkbTransaction]:
    account_ids = [row.id for row in db.query(DkbAccount.id).filter(DkbAccount.user_id == user.id).all()]
    if not account_ids:
        return []
    return db.query(DkbTransaction).filter(DkbTransaction.account_id.in_(account_ids)).all()


@router.get("/positions", response_model=None)
def positions(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[DkbPosition]:
    account_ids = [row.id for row in db.query(DkbAccount.id).filter(DkbAccount.user_id == user.id).all()]
    if not account_ids:
        return []
    return db.query(DkbPosition).filter(DkbPosition.account_id.in_(account_ids)).all()


class PositionCostIn(BaseModel):
    einstandswert_eur: Decimal = Field(gt=0, description="Total cost of the units held now, as the DKB app shows it.")


@router.put("/positions/{position_id}/cost", response_model=None)
def set_position_cost(
    position_id: str,
    payload: PositionCostIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Store the DKB-app Einstandswert of a position as an opening FIFO lot (FinTS may not deliver one)."""
    from app.foundation.portfolio.cost_basis import set_manual_cost

    try:
        result = set_manual_cost(db, user.id, position_id, payload.einstandswert_eur)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Unknown DKB position")
    return {**result, "estimate": True, "not_tax_advice": True}


@router.delete("/positions/{position_id}/cost", response_model=None)
def clear_position_cost(
    position_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    from app.foundation.portfolio.cost_basis import clear_manual_cost

    if not clear_manual_cost(db, user.id, position_id):
        raise HTTPException(status_code=404, detail="Unknown DKB position")
    return {"cleared": True}


def _diagnostic_to_dict(row: DkbDiagnosticRun) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": row.id,
        "provider": row.provider,
        "status": row.status,
        "summary": row.summary,
        "steps": json.loads(row.steps_json or "[]"),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    if row.debug_log:
        result["debug_log"] = row.debug_log
    return result


# ---------------------------------------------------------------------------
# FinTS live self-test -- verifies credentials reach DKB without a full sync
# ---------------------------------------------------------------------------


@router.post("/diagnostics/fints/selftest")
def fints_selftest(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    use_test_product_id: bool = False,
) -> dict[str, Any]:
    import importlib.util

    public = get_public_settings(db)
    secret = get_secret(db, "dkb")
    meta = secret[1] if secret else {}
    product_id = public.get("dkb_product_id") or meta.get("product_id")
    username = meta.get("username")
    pin = secret[0] if secret else None
    fints_url = public.get("dkb_fints_url") or _FINTS_DEFAULT_URL
    blz = public.get("dkb_blz") or "12030000"
    fints_system_id = meta.get("fints_system_id_test" if use_test_product_id else "fints_system_id")

    steps: list[dict[str, Any]] = []

    def step(key: str, label: str, status: str, message: str) -> None:
        steps.append({"key": key, "label": label, "status": status, "message": message})

    # If the test product ID flag is set, override the configured product_id
    # with the community-known public one for this single self-test.
    if use_test_product_id:
        product_id = DKB_PUBLIC_TEST_PRODUCT_ID
        step(
            "test_product_id",
            "Test product ID",
            "passed",
            "Using known-working test product ID to verify connectivity. Your own ID is not affected.",
        )

    debug_log_text: str = ""

    if not (secret and username and pin):
        step("credentials", "Credentials", "failed", "DKB username and PIN must be saved before running the self-test.")
        overall = "failed"
    elif importlib.util.find_spec("fints") is None:
        step("package", "python-fints", "failed", "python-fints is not installed in the backend environment.")
        overall = "failed"
    else:
        step("credentials", "Credentials", "passed", f"Username present, PIN encrypted. Endpoint: {fints_url}")
        if not product_id:
            product_id = DKB_PUBLIC_TEST_PRODUCT_ID
            step("product_id", "FinTS product ID", "passed", "No product ID configured -- using built-in default (DKB does not require registration).")
        else:
            step("product_id", "FinTS product ID", "passed", "Product ID is configured.")
        step("package", "python-fints", "passed", "python-fints is installed.")

        # Real authenticated probe: opens a live FinTS dialog and drives the
        # DKB-App decoupled push to completion (one push to approve), then lists
        # SEPA accounts. This surfaces the true outcome instead of guessing from
        # an anonymous BPD enumeration that never validates the PIN.
        adapter = DkbFinTSAdapter(
            _safe_fints_url(fints_url),
            blz,
            username,
            pin,
            tan_security_function=public.get("dkb_tan_security_function") or meta.get("tan_security_function"),
            tan_medium=public.get("dkb_tan_medium") or meta.get("tan_medium"),
            product_id=product_id,
            system_id=fints_system_id,
            push_timeout_seconds=int(public.get("dkb_push_timeout_seconds") or 120),
            poll_interval_seconds=int(public.get("dkb_poll_interval_seconds") or 5),
        )
        adapter.debug_fints_logging = bool(public.get("dkb_debug_fints_logging", False))
        try:
            result = adapter.verify_login()
            methods = result.get("tan_methods") or []
            label = ", ".join(m.get("name", "") for m in methods) or "(none enumerated)"
            step(
                "tan_discovery",
                "TAN mechanism",
                "passed",
                f"DKB-App (decoupled) used. Methods: {label}",
            )
            step(
                "login",
                "Authenticated login",
                "passed",
                f"Login confirmed -- {result.get('accounts', 0)} account(s) reachable. "
                "Approving the DKB-App push completes the dialog.",
            )
            overall = "passed"

            # Persist system_id from a successful dialog so subsequent
            # connections skip the anonymous BPD discovery. For test-PID runs
            # the key is fints_system_id_test so the real system_id is untouched.
            new_system_id = result.get("system_id")
            if new_system_id and new_system_id != fints_system_id:
                meta_key = "fints_system_id_test" if use_test_product_id else "fints_system_id"
                from app.foundation.settings import update_secret_meta
                update_secret_meta(db, "dkb", {meta_key: new_system_id})
                step(
                    "system_id",
                    "System ID",
                    "passed",
                    "System ID persisted for subsequent sessions.",
                )
        except DkbFinTSConnectionRejected:
            step(
                "connection",
                "FinTS connection",
                "failed",
                "DKB rejected the FinTS connection (HTTP 400 / could not establish system_id). "
                "Check that username and PIN are correct, and that BLZ is 12030000. "
                "DKB does not validate product_id -- the built-in default always works. "
                "Enable debug FinTS logging in Settings to see the full wire-level error.",
            )
            overall = "failed"
        except DkbFinTSManualTanRequired as exc:
            step(
                "tan_discovery",
                "TAN mechanism",
                "warning",
                f"{exc} Enable DKB-App under Sicherheit > TAN-Verfahren > DKB-App freischalten.",
            )
            overall = "warning"
        except DkbFinTSTanTimeout:
            step(
                "login",
                "Authenticated login",
                "warning",
                "A DKB-App push was sent but not approved in time. Retry and approve the push promptly.",
            )
            overall = "warning"
        except Exception as exc:
            _api_logger.error("DKB FinTS self-test connection error", exc_info=True)
            detail = _sanitize_fints_error(str(exc), pin, username)
            step(
                "connection",
                "FinTS connection",
                "failed",
                f"FinTS connection failed: {detail}" if detail else "FinTS connection failed -- see server logs for details.",
            )
            overall = "failed"
        finally:
            raw_log: list[str] = getattr(adapter, "debug_log", []) or []
            trimmed = raw_log[-120:]
            debug_log_text = "\n".join(trimmed)
            if len(debug_log_text) > 4000:
                debug_log_text = "...\n" + debug_log_text[-3997:]
            if trimmed:
                step("debug_log", "FinTS exchange", "passed", debug_log_text)

    from app.foundation.dkb import _ascii_safe

    summary = (
        "FinTS self-test passed -- DKB-App is available for decoupled sync."
        if overall == "passed"
        else "FinTS self-test completed with warnings -- check steps above."
        if overall == "warning"
        else "FinTS self-test failed -- fix the issues above before syncing."
    )
    row = DkbDiagnosticRun(
        user_id=user.id,
        provider="fints",
        status=overall,
        summary=_ascii_safe(summary),
        steps_json=_ascii_safe(json.dumps(steps)),
        debug_log=_ascii_safe(debug_log_text) or None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _diagnostic_to_dict(row)
