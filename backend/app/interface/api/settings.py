import re
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.foundation.core.db import get_db
from app.foundation.core.net import pin_url_to_ip, validate_outbound_url
from app.foundation.models.entities import User
from app.foundation.schemas import IntegrationTestRequest, IntegrationTestResponse, SettingsPayload
from app.foundation.auth import current_user
from app.foundation.broker_status import owner_user_id
from app.foundation.settings import (
    DEFAULT_PUBLIC_SETTINGS,
    SENSITIVE_INTEGRATIONS,
    delete_public_setting,
    get_connection_tests,
    get_public_settings,
    get_secret,
    integration_presence,
    normalize_base_url,
    probe_root,
    record_connection_test,
    resolve_frontend_origins,
    resolve_llm_base_url,
    resolve_llm_model,
    resolve_openbb_url,
    resolve_obsidian_url,
    set_secret,
    update_secret_meta,
    upsert_public_settings,
)
from app.foundation.settings_catalog import PAGES, get_catalog, get_connections, validate_public_settings
from app.foundation.email import send_test_email
from app.foundation.providers.alphavantage_provider import AlphaVantageProvider
from app.foundation.providers.databento_provider import DatabentoProvider
from app.foundation.providers.ecb_provider import EcbProvider
from app.foundation.providers.eod_provider import EodProvider
from app.foundation.providers.finnhub_provider import FinnhubProvider
from app.foundation.providers.justetf_provider import JustETFProvider
from app.foundation.providers.massive_provider import MassiveProvider
from app.foundation.providers.tiingo_provider import TiingoProvider
from app.foundation.providers.twelvedata_provider import TwelveDataProvider
from app.foundation.providers.yfinance_provider import YFinanceProvider
from app.foundation.providers.registry import build_provider_registry
from app.foundation.providers import invalidate_registry_cache
from app.foundation.tax_allowances import BANK_SETTING_KEYS, BankSettingsOwnedByOtherUser, claim_bank_settings

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _integrations_payload(db: Session) -> dict[str, dict[str, Any]]:
    """Per service: whether a credential is stored, and the last saved-config test."""
    tests = get_connection_tests(db)
    payload: dict[str, dict[str, Any]] = {
        key: {"configured": value, "last_test": tests.get(key)} for key, value in integration_presence(db).items()
    }
    for service, result in tests.items():
        payload.setdefault(service, {"configured": False, "last_test": result})
    return payload

QUOTE_PROVIDERS = {
    "alphavantage": AlphaVantageProvider,
    "finnhub": FinnhubProvider,
    "eod": EodProvider,
    "twelvedata": TwelveDataProvider,
    "databento": DatabentoProvider,
    "tiingo": TiingoProvider,
}

# Each provider is probed with a symbol it actually covers: the free tiers of
# the US-focused providers know no XETRA listing, and a "not covered" answer
# says nothing about the key. EODHD is not probed at all: its free plan is 20
# calls a day, shared with ingestion, so a test would only burn quota.
QUOTE_PROBE_SYMBOLS = {
    "alphavantage": "AAPL",
    "finnhub": "AAPL",
    "twelvedata": "AAPL",
    "databento": "AAPL",
    "tiingo": "AAPL",
}
_HTTP_STATUS = re.compile(r"HTTP (\d{3})")
# Failures that say nothing against the connection itself.
_HARMLESS_REASONS = ("not_applicable", "rate_limited")


def _reason_from_text(text: str) -> str:
    """Last resort for a provider failure that carries no reason code: its HTTP status."""
    match = _HTTP_STATUS.search(text)
    if match:
        status = int(match.group(1))
        if status in (401, 403):
            return "auth"
        if status == 429:
            return "rate_limited"
        return "http_error"
    return "no_data"


def _quote_probe_outcome(result: dict[str, Any]) -> tuple[bool, str, str | None]:
    """Turn a provider quote answer into (connection ok, message, reason code).

    Only a real failure (auth, network, HTTP error) is a failed connection. A
    throttle or a coverage gap means the key reached the provider.
    """
    if result.get("ok"):
        return True, "Provider quote available.", None
    text = str(result.get("error") or "; ".join(result.get("quality", {}).get("warnings", [])) or "No quote returned.")
    reason = result.get("reason") or _reason_from_text(text)
    if reason == "rate_limited":
        return True, "Key accepted; the provider is rate-limiting requests right now.", reason
    if reason == "not_applicable":
        return True, "Key accepted; this provider does not cover the test symbol.", reason
    return False, text, reason


def _reason_from_exception(exc: Exception) -> str:
    if isinstance(exc, (httpx.TransportError, TimeoutError, ConnectionError)):
        return "network"
    return _reason_from_text(str(exc)) if "HTTP" in str(exc) else "network"


# Providers whose self-test needs no key/secret — just import + status().
NO_KEY_STATUS_PROVIDERS = {
    "ecb_sdw": EcbProvider,
    "yfinance": YFinanceProvider,
    "justetf": JustETFProvider,
}


@router.get("/schema")
def settings_schema(
    _user: User = Depends(current_user),
) -> dict:
    """Return the settings catalog — metadata for every setting and integration.

    This is the single source of truth the Control Center UI uses to render
    settings panels. Every key in ``DEFAULT_PUBLIC_SETTINGS`` and every
    ``SENSITIVE_INTEGRATIONS`` member has a corresponding catalog entry.
    """
    catalog = get_catalog()
    for key, entry in catalog.items():
        entry["default"] = None if entry["sensitive"] else DEFAULT_PUBLIC_SETTINGS.get(key)
    completeness = {
        "public_setting_keys": list(DEFAULT_PUBLIC_SETTINGS.keys()),
        "sensitive_integration_keys": sorted(SENSITIVE_INTEGRATIONS),
    }
    return {"catalog": catalog, "pages": PAGES, "connections": get_connections(), "completeness": completeness}


class AttentionItemOut(BaseModel):
    id: str
    severity: Literal["error", "warning", "info"]
    title: str
    detail: str
    href: str
    action: str


class AttentionResponse(BaseModel):
    items: list[AttentionItemOut]
    # Keys saved after the running worker started (applied on its next restart).
    pending_restart: list[str]


@router.get("/attention", response_model=AttentionResponse)
def settings_attention(
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> AttentionResponse:
    """What needs the owner's attention, most severe first (Control Center landing)."""
    from app.foundation.control_center import attention_items, pending_restart_keys, worker_heartbeat

    return AttentionResponse(
        items=[AttentionItemOut(**item) for item in attention_items(db)],
        pending_restart=pending_restart_keys(db, worker_heartbeat(db)),
    )


@router.get("", response_model=SettingsPayload)
def read_settings(
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> SettingsPayload:
    settings = get_public_settings(db)
    settings["llm_base_url"] = resolve_llm_base_url(db)
    settings["llm_model"] = resolve_llm_model(db)
    settings["openbb_api_url"] = resolve_openbb_url(db)
    settings["obsidian_rest_url"] = resolve_obsidian_url(db)
    settings["frontend_origin"] = ",".join(resolve_frontend_origins(db))
    # Surface the DKB username from encrypted meta (it is not stored as a public
    # setting); overrides any legacy plaintext mirror so the UI keeps prefilling.
    dkb_secret = get_secret(db, "dkb")
    settings["dkb_username"] = dkb_secret[1].get("username") if dkb_secret else None
    return SettingsPayload(settings=settings, integrations=_integrations_payload(db))


@router.put("", response_model=SettingsPayload)
def update_settings(
    payload: SettingsPayload,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> SettingsPayload:
    # The DKB username is not the secret, but it must never be persisted as a
    # plaintext AppSetting — it belongs in the encrypted "dkb" meta alongside the
    # PIN. Strip it from public settings and route it into encrypted meta below.
    public_settings = dict(payload.settings)
    errors = validate_public_settings(public_settings)
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})
    dkb_username = public_settings.pop("dkb_username", None)
    # A key named after a secret integration is a credential: it goes through
    # `integrations` (encrypted) or nowhere, never into plaintext app_settings.
    for service in SENSITIVE_INTEGRATIONS:
        public_settings.pop(service, None)
    # Only documented settings are writable here. Internal state that shares the
    # table (worker heartbeat, connection-test results, regime snapshot, the
    # Telegram update offset, ...) is written by the code that owns it through
    # upsert_public_settings, never by a client.
    unknown = sorted(key for key in public_settings if key not in DEFAULT_PUBLIC_SETTINGS)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={"errors": {key: "unknown setting" for key in unknown}},
        )
    unknown_services = sorted(service for service in payload.integrations if service not in SENSITIVE_INTEGRATIONS)
    if unknown_services:
        raise HTTPException(
            status_code=400,
            detail={"errors": {service: "unknown integration" for service in unknown_services}},
        )
    # The server has one Scalable login, bound to the user it syncs into; nobody
    # else may switch it off, point it at another portfolio or loosen its guard.
    current = get_public_settings(db)
    scalable_changes = sorted(
        key for key, value in public_settings.items() if key.startswith("scalable_") and current.get(key) != value
    )
    if scalable_changes:
        owner = owner_user_id(db)
        if owner is not None and owner != user.id:
            raise HTTPException(
                status_code=403,
                detail="Only the user Scalable Capital syncs into can change its settings",
            )
    # The per-bank Freistellungsauftrag/NV values describe one person's accounts.
    if any(key in public_settings for key in BANK_SETTING_KEYS):
        try:
            public_settings = claim_bank_settings(db, user.id, public_settings)
        except BankSettingsOwnedByOtherUser as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    telegram_changed = "telegram_mode" in public_settings or "telegram" in payload.integrations
    settings = upsert_public_settings(db, public_settings)
    delete_public_setting(db, "dkb_username")  # remove any legacy plaintext mirror

    for service, config in payload.integrations.items():
        secret = config.get("secret")
        meta = {key: value for key, value in config.items() if key != "secret"}
        if secret:
            set_secret(db, service, secret, meta)
        elif meta:
            update_secret_meta(db, service, meta)

    # Fold a username-only update (PIN not re-entered) into encrypted dkb meta.
    # No-ops if no "dkb" secret exists yet (update_secret_meta returns early).
    if dkb_username:
        update_secret_meta(db, "dkb", {"username": dkb_username})

    # Webhook mode needs the secret Telegram signs its calls with; without it the
    # endpoint answers 403 forever. Prepare it (and register the URL) whenever
    # the mode or the bot token was just saved.
    if telegram_changed and settings.get("telegram_mode") == "webhook":
        from app.foundation.telegram_updates import sync_webhook

        sync_webhook(db)

    invalidate_registry_cache()

    return SettingsPayload(settings=settings, integrations=_integrations_payload(db))


@router.post("/integrations/test", response_model=IntegrationTestResponse)
def test_integration(
    payload: IntegrationTestRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> IntegrationTestResponse:
    """Probe a service and remember the outcome for the Control Center.

    A probe of an unsaved value (``payload.value`` set) is not recorded: it
    says nothing about the credential the app actually uses.
    """
    if payload.service == "scalable":
        # The probe runs the server-wide sc session: the same owner rule as /api/scalable/test.
        from app.interface.api.scalable import require_connection_owner

        require_connection_owner(db, user)
    result = _probe_integration(payload, db)
    if payload.value is None or payload.service == "smtp":
        result.tested_at = record_connection_test(
            db, result.service, result.ok, result.message, reason=result.reason, probe_symbol=result.probe_symbol,
        )
    return result


def _probe_integration(payload: IntegrationTestRequest, db: Session) -> IntegrationTestResponse:
    if payload.service == "scalable":
        from app.foundation.scalable.service import probe as scalable_probe

        result = scalable_probe(db)
        failed = next((step for step in result["steps"] if not step["ok"]), None)
        message = result["message"] if failed is None else f"{failed['name']}: {failed['detail']}"
        return IntegrationTestResponse(service="scalable", ok=bool(result["ok"]), message=message[:500])

    if payload.service == "dkb":
        try:
            secret_parts = get_secret(db, "dkb")
            if not secret_parts:
                return IntegrationTestResponse(
                    service="dkb",
                    ok=False,
                    message="DKB credentials not saved. Save username and PIN before testing.",
                )
            _pin, meta = secret_parts
            username = meta.get("username", "")
            if not username:
                return IntegrationTestResponse(
                    service="dkb",
                    ok=False,
                    message="DKB username not found in encrypted credentials.",
                )
            return IntegrationTestResponse(
                service="dkb",
                ok=True,
                message=(
                    f"DKB credentials present (user: {username}). "
                    "Use the FinTS self-test diagnostic to verify live connectivity."
                ),
            )
        except Exception as exc:
            return IntegrationTestResponse(service="dkb", ok=False, message=str(exc))

    if payload.service == "llm":
        try:
            base = payload.meta.get("base_url") or resolve_llm_base_url(db)
            probe = probe_root(normalize_base_url(base))
            health_url, resolved_ip = validate_outbound_url(f"{probe}/health", allow_private=True)
            pinned_url, hostname, extensions = pin_url_to_ip(health_url, resolved_ip)
            with httpx.Client(timeout=3.0) as client:
                resp = client.get(pinned_url, headers={"Host": hostname}, extensions=extensions)
            ok = resp.status_code == 200
            message = f"Available at {probe}." if ok else f"Unavailable ({resp.status_code})."
            return IntegrationTestResponse(service="llm", ok=ok, message=message)
        except ValueError as exc:
            return IntegrationTestResponse(service="llm", ok=False, message=str(exc))
        except Exception:
            return IntegrationTestResponse(service="llm", ok=False, message="LLM connection test failed.")

    if payload.service == "openbb":
        try:
            url = payload.meta.get("api_url") or resolve_openbb_url(db)
            url, resolved_ip = validate_outbound_url(url, allow_private=True)
            pinned_url, hostname, extensions = pin_url_to_ip(url, resolved_ip)
            with httpx.Client(timeout=3.0) as client:
                resp = client.get(pinned_url, headers={"Host": hostname}, extensions=extensions)
            ok = resp.status_code < 500
            if not ok:
                return IntegrationTestResponse(
                    service="openbb",
                    ok=False,
                    message=f"Unreachable ({resp.status_code}).",
                )
            # Server is up — probe which ODP providers are actually available.
            from app.foundation.providers.openbb_provider import OpenBBProvider
            probe_provider = OpenBBProvider(enabled=True, api_url=url)
            probe_result = probe_provider.probe_providers()
            available = probe_result.get("available", [])
            unavailable = probe_result.get("unavailable", [])
            parts = [f"Reachable at {url}."]
            if available:
                parts.append(f"Available ODP providers: {', '.join(available)}.")
            if unavailable:
                parts.append(f"Unavailable: {', '.join(unavailable)}.")
            if not available:
                parts.append("No providers responded — install at least openbb-yfinance.")
            return IntegrationTestResponse(service="openbb", ok=True, message=" ".join(parts))
        except ValueError as exc:
            return IntegrationTestResponse(service="openbb", ok=False, message=str(exc))
        except Exception:
            return IntegrationTestResponse(service="openbb", ok=False, message="OpenBB connection test failed.")

    if payload.service == "obsidian":
        try:
            url = payload.meta.get("rest_url") or resolve_obsidian_url(db)
            url, resolved_ip = validate_outbound_url(url, allow_private=True)
            pinned_url, hostname, extensions = pin_url_to_ip(url, resolved_ip)
            with httpx.Client(timeout=3.0) as client:
                resp = client.get(
                    pinned_url,
                    headers={"Host": hostname, "Authorization": f"Bearer {payload.value or ''}"},
                    extensions=extensions,
                )
            ok = resp.status_code == 200
            message = "Reachable." if ok else f"Unreachable ({resp.status_code})."
            return IntegrationTestResponse(service="obsidian", ok=ok, message=message)
        except ValueError as exc:
            return IntegrationTestResponse(service="obsidian", ok=False, message=str(exc))
        except Exception:
            return IntegrationTestResponse(service="obsidian", ok=False, message="Obsidian connection test failed.")

    if payload.service == "anthropic":
        try:
            key = payload.value or (get_secret(db, "anthropic") or ("", {}))[0]
            with httpx.Client(timeout=3.0) as client:
                resp = client.get(
                    "https://api.anthropic.com/v1/models",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"}
                )
            ok = resp.status_code in (200, 403)
            message = "Reachable." if ok else f"Unreachable ({resp.status_code})."
            return IntegrationTestResponse(service="anthropic", ok=ok, message=message)
        except Exception as exc:
            return IntegrationTestResponse(service="anthropic", ok=False, message=str(exc))

    if payload.service == "openai":
        try:
            key = payload.value or (get_secret(db, "openai") or ("", {}))[0]
            with httpx.Client(timeout=3.0) as client:
                resp = client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {key}"}
                )
            ok = resp.status_code == 200
            message = "Reachable." if ok else f"Unreachable ({resp.status_code})."
            return IntegrationTestResponse(service="openai", ok=ok, message=message)
        except Exception as exc:
            return IntegrationTestResponse(service="openai", ok=False, message=str(exc))

    if payload.service in QUOTE_PROVIDERS:
        probe_symbol = QUOTE_PROBE_SYMBOLS.get(payload.service)
        try:
            saved_secret = get_secret(db, payload.service)
            key = payload.value if payload.value is not None else (saved_secret[0] if saved_secret else None)
            if payload.service == "eod":
                # Configured status only: a real call would spend the 20/day quota.
                if key:
                    return IntegrationTestResponse(
                        service="eod", ok=True, message="API key saved (not tested, to keep the daily quota).",
                        reason="not_applicable", probe_symbol="configured",
                    )
                return IntegrationTestResponse(
                    service="eod", ok=False, message="No API key saved.", reason="auth", probe_symbol="configured",
                )
            prov = QUOTE_PROVIDERS[payload.service](key)
            result = prov.get_quote(probe_symbol or "AAPL")
            ok, message, reason = _quote_probe_outcome(result)
            return IntegrationTestResponse(
                service=payload.service, ok=ok, message=message, reason=reason, probe_symbol=probe_symbol,
            )
        except Exception as exc:
            return IntegrationTestResponse(
                service=payload.service, ok=False, message=str(exc), reason=_reason_from_exception(exc),
                probe_symbol=probe_symbol,
            )

    if payload.service == "fred":
        try:
            registry = build_provider_registry(db)
            prov = next((p for p in registry.providers if p.name == payload.service), None)
            if prov is None:
                return IntegrationTestResponse(service=payload.service, ok=False, message="Provider not available.")
            result = prov.get_macro_indicators()
            ok = bool(result.get("ok"))
            message = (
                "Provider macro data available." if ok
                else result.get("error") or "; ".join(result.get("quality", {}).get("warnings", []))
            )
            return IntegrationTestResponse(service=payload.service, ok=ok, message=message)
        except Exception as exc:
            return IntegrationTestResponse(service=payload.service, ok=False, message=str(exc))

    if payload.service == "massive":
        try:
            saved_secret = get_secret(db, "massive")
            key = payload.value if payload.value is not None else (saved_secret[0] if saved_secret else None)
            prov = MassiveProvider(key)
            result = prov.status()
            ok = bool(result.get("available"))
            message = result.get("message", "Massive status check completed.")
            return IntegrationTestResponse(service="massive", ok=ok, message=message)
        except Exception as exc:
            return IntegrationTestResponse(service="massive", ok=False, message=str(exc))

    if payload.service == "alpaca":
        try:
            saved_secret = get_secret(db, "alpaca")
            if saved_secret:
                api_key, meta = saved_secret
                api_secret = meta.get("api_secret", "")
            else:
                api_key = None
                api_secret = ""
            key = payload.value if payload.value is not None else api_key
            meta_secret = (payload.meta.get("api_secret") or api_secret) if payload.meta else api_secret
            if not key or not meta_secret:
                return IntegrationTestResponse(
                    service="alpaca",
                    ok=False,
                    message="Alpaca requires both an API key and a secret key. Enter the secret as meta field 'api_secret'.",
                )
            from app.foundation.providers.alpaca_provider import AlpacaProvider
            prov = AlpacaProvider(key, meta_secret)
            result = prov.status()
            ok = bool(result.get("available"))
            message = result.get("message", "Alpaca status check completed.")
            return IntegrationTestResponse(service="alpaca", ok=ok, message=message)
        except Exception as exc:
            return IntegrationTestResponse(service="alpaca", ok=False, message=str(exc))

    if payload.service in NO_KEY_STATUS_PROVIDERS:
        try:
            prov = NO_KEY_STATUS_PROVIDERS[payload.service]()
            result = prov.status()
            ok = bool(result.get("available"))
            message = result.get("message", "Status check completed.")
            return IntegrationTestResponse(service=payload.service, ok=ok, message=message)
        except Exception as exc:
            return IntegrationTestResponse(service=payload.service, ok=False, message=str(exc))

    if payload.service == "rss":
        try:
            from app.foundation.rss_provider import fetch_feed
            url = payload.meta.get("url") or payload.value
            if not url:
                return IntegrationTestResponse(
                    service="rss",
                    ok=False,
                    message="No RSS feed URL provided. Pass it in meta.url or value.",
                )
            result = fetch_feed(url)
            ok = bool(result.get("ok"))
            message = (
                f"Feed fetched ({len(result.get('items', []))} items)."
                if ok
                else (result.get("error") or "Feed fetch failed.")
            )
            return IntegrationTestResponse(service="rss", ok=ok, message=message)
        except Exception as exc:
            return IntegrationTestResponse(service="rss", ok=False, message=str(exc))

    if payload.service == "smtp":
        try:
            # value is an optional recipient; anything else (the old UI sent the
            # typed SMTP password here) falls back to the From address.
            to_address = payload.value if payload.value and "@" in payload.value else None
            if not to_address:
                public = get_public_settings(db)
                to_address = public.get("smtp_from_address") or "noreply@quantfolio.local"
            ok, message = send_test_email(db, to_address)
            return IntegrationTestResponse(service="smtp", ok=ok, message=message)
        except Exception as exc:
            return IntegrationTestResponse(service="smtp", ok=False, message=str(exc))

    if payload.service == "telegram":
        token = payload.value or (get_secret(db, "telegram") or ("", {}))[0]
        if not token:
            return IntegrationTestResponse(service="telegram", ok=False, message="No bot token saved.")
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(f"https://api.telegram.org/bot{token}/getMe")
                resp.raise_for_status()
                data = resp.json()
            if data.get("ok"):
                username = data.get("result", {}).get("username", "?")
                return IntegrationTestResponse(service="telegram", ok=True, message=f"Connected as @{username}")
            return IntegrationTestResponse(service="telegram", ok=False, message="Telegram API rejected the token.")
        except httpx.HTTPStatusError as exc:
            # Do not use str(exc)/{exc}: httpx.HTTPStatusError's default message
            # embeds the full request URL, which contains the bot token
            # (Telegram authenticates via URL path, not headers).
            return IntegrationTestResponse(
                service="telegram",
                ok=False,
                message=f"Connection failed: HTTP {exc.response.status_code} from Telegram API",
            )
        except Exception as exc:
            return IntegrationTestResponse(
                service="telegram", ok=False, message=f"Connection failed: {type(exc).__name__}"
            )

    return IntegrationTestResponse(
        service=payload.service,
        ok=False,
        message=f"Unsupported integration: {payload.service}",
    )


@router.get("/provider-chain")
def get_provider_chain(
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> dict:
    """Return the current provider chain and the status of each provider."""
    registry = build_provider_registry(db)
    return {"providers": registry.status()}
