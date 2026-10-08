"""Control Center "Needs attention" list.

One place that turns scattered health signals into a short, actionable list:
a dead worker, settings waiting for a worker restart, failing connections,
stale research extracts, an expiring NV certificate, and settings whose value
silently does nothing. Each item links to the page that fixes it.

Only reads state other code already records; never probes a network service.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.foundation.models.entities import ApiKey, AppSetting, DkbSyncLog, JobRun, ProviderHealth
from app.foundation.settings import get_connection_tests, get_public_settings, get_setting_row
from app.foundation.settings_catalog import CATALOG, CONNECTIONS, PAGE_PATHS

logger = logging.getLogger(__name__)

Severity = Literal["error", "warning", "info"]
_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

# Provider-health rows older than this describe a probe nobody re-ran; showing
# them as current problems was one source of the old page's stale data.
PROVIDER_HEALTH_MAX_AGE = timedelta(days=7)
NV_WARN_DAYS = 60
DKB_SYNC_STALE_DAYS = 30
ALPHACRAFTER_MIN_BASKET = 30


@dataclass
class AttentionItem:
    id: str
    severity: Severity
    title: str
    detail: str
    href: str
    action: str


def _page(group: str, connection: str | None = None) -> str:
    href = f"/settings/{PAGE_PATHS[group]}"
    return f"{href}?connection={connection}" if connection else href


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def worker_heartbeat(db: Session) -> dict[str, Any] | None:
    from app.foundation.jobs import WORKER_HEARTBEAT_KEY

    beat = get_setting_row(db, WORKER_HEARTBEAT_KEY)
    return beat if isinstance(beat, dict) else None


def pending_restart_keys(db: Session, heartbeat: dict[str, Any] | None) -> list[str]:
    """``needs_worker_restart`` settings saved after the running worker started."""
    started = _parse_dt(heartbeat.get("started_at")) if heartbeat else None
    if started is None:
        return []
    keys = [key for key, entry in CATALOG.items() if entry.status == "needs_worker_restart"]
    rows = db.query(AppSetting.key, AppSetting.updated_at).filter(AppSetting.key.in_(keys)).all()
    pending = []
    for key, updated_at in rows:
        if updated_at is None:
            continue
        updated = updated_at if updated_at.tzinfo else updated_at.replace(tzinfo=UTC)
        if updated > started:
            pending.append(key)
    return sorted(pending)


def attention_items(db: Session, *, today: date | None = None) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    clock = today or now.date()
    settings = get_public_settings(db)
    items: list[AttentionItem] = []

    heartbeat = worker_heartbeat(db)
    _worker_items(db, heartbeat, now, items)
    _connection_items(db, now, items)
    _job_items(db, heartbeat, items)
    _extract_items(clock, items)
    _dkb_items(db, now, items)
    _scalable_items(db, settings, items)
    _setting_items(settings, clock, items)

    items.sort(key=lambda item: _SEVERITY_ORDER[item.severity])
    return [asdict(item) for item in items]


def _worker_items(db: Session, heartbeat: dict[str, Any] | None, now: datetime, items: list[AttentionItem]) -> None:
    if heartbeat is None:
        items.append(AttentionItem(
            "worker-unknown", "info", "Background worker hasn't reported yet",
            "Schedules and restart tracking appear once a worker with this version is running.",
            "/settings/status", "View status",
        ))
        return
    beat_at = _parse_dt(heartbeat.get("beat_at"))
    interval = int(heartbeat.get("interval_minutes", 5))
    if beat_at is None or now - beat_at > timedelta(minutes=3 * interval):
        items.append(AttentionItem(
            "worker-down", "error", "Background worker is not running",
            "Last heartbeat " + (beat_at.strftime("%Y-%m-%d %H:%M UTC") if beat_at else "unknown")
            + ". Scheduled jobs (prices, regime, Discover) are not running.",
            "/settings/status", "View status",
        ))
    pending = pending_restart_keys(db, heartbeat)
    if pending:
        labels = ", ".join(CATALOG[key].label for key in pending)
        items.append(AttentionItem(
            "restart-pending", "warning", "Waiting for a worker restart",
            f"Saved but not active yet: {labels}. Restart quantfolio-worker to apply.",
            "/settings/status", "View status",
        ))


# Optional market-data providers: the app runs on yfinance without them, so a
# problem here is information unless the key itself is refused.
_OPTIONAL_DATA_PROVIDERS = {
    "alphavantage", "finnhub", "eod", "twelvedata", "databento", "tiingo", "alpaca", "massive", "fred",
}
# Providers whose connection probe changed (it used to ask every one for the
# XETRA ETF EUNL.DE). A stored result without a probe symbol is from the old
# probe and says nothing about the key.
_PROBE_CHANGED = {"alphavantage", "finnhub", "eod", "twelvedata", "databento", "tiingo"}
# Reason codes (see providers.base.PROBE_REASONS) that are not a broken connection.
_HARMLESS_REASONS = {"not_applicable", "rate_limited"}
# Legacy fallback, ONLY for stored results with no reason code from a service
# whose probe never changed.
_LEGACY_BENIGN_MARKERS = ("rate limit", "429", "us listings only", "skipping")
_LEGACY_AUTH_MARKERS = ("401", "403", "unauthor", "forbidden", "invalid api key")


def _failure_reason(service: str, result: dict[str, Any]) -> str:
    """The structured reason of a failed test, or ``legacy`` / a best guess for old results."""
    code = result.get("reason")
    if isinstance(code, str) and code:
        return code
    if service in _PROBE_CHANGED and not result.get("probe_symbol"):
        return "legacy"
    lowered = str(result.get("message") or "").lower()
    if any(marker in lowered for marker in _LEGACY_BENIGN_MARKERS):
        return "not_applicable"
    if any(marker in lowered for marker in _LEGACY_AUTH_MARKERS):
        return "auth"
    return "unknown"


def _failure_severity(service: str, reason: str) -> Severity:
    """Warning only for a refused key, or a real failure of a non-optional service."""
    if reason == "auth":
        return "warning"
    if reason in ("legacy", "no_data") or service in _OPTIONAL_DATA_PROVIDERS:
        return "info"
    return "warning"


def _loads_meta(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _connection_items(db: Session, now: datetime, items: list[AttentionItem]) -> None:
    tests = get_connection_tests(db)
    configured = {row.service for row in db.query(ApiKey.service).all()}
    flagged: set[str] = set()
    for service, result in tests.items():
        connection = CONNECTIONS.get(service)
        if connection is None or result.get("ok"):
            continue
        if connection.secret and connection.secret not in configured:
            continue  # tested, then the credential was removed
        reason = _failure_reason(service, result)
        if reason in _HARMLESS_REASONS:
            continue  # a throttle or a coverage gap, not a broken connection
        flagged.add(service)
        legacy = reason == "legacy"
        items.append(AttentionItem(
            f"connection-{service}", _failure_severity(service, reason),
            f"{connection.label}: not tested yet" if legacy else f"{connection.label}: last test failed",
            "The saved result is from an older test. Run the test again." if legacy
            else str(result.get("message") or "The connection test failed."),
            _page(connection.group, service), "Fix",
        ))
    cutoff = now - PROVIDER_HEALTH_MAX_AGE
    rows = (
        db.query(ProviderHealth)
        .filter(ProviderHealth.capability == "status", ProviderHealth.configured.is_(True),
                ProviderHealth.available.is_(False))
        .all()
    )
    for row in rows:
        updated = row.updated_at if row.updated_at is None or row.updated_at.tzinfo else row.updated_at.replace(tzinfo=UTC)
        connection = CONNECTIONS.get(row.provider)
        if connection is None or row.provider in flagged or updated is None or updated < cutoff:
            continue
        meta = _loads_meta(row.meta_json)
        stored = meta.get("reason")
        reason = stored if isinstance(stored, str) else _failure_reason(
            row.provider, {"message": row.message, "probe_symbol": "n/a"}
        )
        if reason in _HARMLESS_REASONS:
            continue
        items.append(AttentionItem(
            f"provider-{row.provider}", _failure_severity(row.provider, reason), f"{connection.label} is unavailable",
            row.message or "The nightly provider probe failed.",
            _page(connection.group, row.provider), "Fix",
        ))


def _job_items(db: Session, heartbeat: dict[str, Any] | None, items: list[AttentionItem]) -> None:
    if heartbeat is None:
        return
    registered = {job["id"] for job in heartbeat.get("jobs", [])}
    newest = (
        db.query(JobRun.job_name, func.max(JobRun.started_at).label("started_at"))
        .group_by(JobRun.job_name)
        .subquery()
    )
    rows = (
        db.query(JobRun.job_name)
        .join(newest, (JobRun.job_name == newest.c.job_name) & (JobRun.started_at == newest.c.started_at))
        .filter(JobRun.status == "error")
        .all()
    )
    failed = sorted(row.job_name for row in rows if row.job_name in registered)
    if failed:
        items.append(AttentionItem(
            "jobs-failed", "warning",
            f"{len(failed)} scheduled job{'s' if len(failed) > 1 else ''} failed on the last run",
            ", ".join(failed), "/settings/status#jobs", "View jobs",
        ))


def _extract_items(clock: date, items: list[AttentionItem]) -> None:
    try:
        from app.foundation.data_engineering.extract_freshness import research_extract_freshness

        extracts = research_extract_freshness(today=clock)
    except Exception:
        logger.warning("Research extract freshness unavailable", exc_info=True)
        return
    stale = [e for e in extracts if e.get("stale")]
    if stale:
        names = ", ".join(str(e.get("label")) for e in stale)
        items.append(AttentionItem(
            "extracts-stale", "warning",
            f"{len(stale)} research extract{'s are' if len(stale) > 1 else ' is'} out of date",
            f"{names}. The Discover signals built on them stay empty until refreshed.",
            "/settings/status#extracts", "How to refresh",
        ))


def _dkb_items(db: Session, now: datetime, items: list[AttentionItem]) -> None:
    if db.query(ApiKey.id).filter(ApiKey.service == "dkb").first() is None:
        items.append(AttentionItem(
            "dkb-missing", "info", "Bank not connected",
            "Connect DKB to sync your accounts and depot (read-only).",
            _page("banking", "dkb"), "Connect",
        ))
        return
    last = db.query(DkbSyncLog).order_by(DkbSyncLog.created_at.desc()).first()
    if last is None:
        return
    created = last.created_at if last.created_at.tzinfo else last.created_at.replace(tzinfo=UTC)
    if last.state in ("failed", "error"):
        items.append(AttentionItem(
            "dkb-failed", "warning", "Last DKB sync failed", last.message[:300],
            _page("banking"), "Open bank",
        ))
    elif now - created > timedelta(days=DKB_SYNC_STALE_DAYS):
        items.append(AttentionItem(
            "dkb-stale", "info", f"No DKB sync for {(now - created).days} days",
            "Holdings and cash are only as current as the last sync.",
            "/portfolio/accounts", "Sync now",
        ))


def _scalable_items(db: Session, settings: dict[str, Any], items: list[AttentionItem]) -> None:
    """Login expired, trade guard missing, stale sync, holdings waiting for review."""
    if not settings.get("scalable_enabled"):
        return
    from app.foundation.models.entities import BrokerSyncLog
    from app.foundation.broker_status import owner_user_id, status as scalable_status

    # One sc login per server, so one status: the owner's, or before the first
    # successful sync whoever tried last.
    user_id = owner_user_id(db) or (
        db.query(BrokerSyncLog.user_id)
        .filter(BrokerSyncLog.source == "scalable")
        .order_by(BrokerSyncLog.started_at.desc())
        .limit(1)
        .scalar()
    )
    state = scalable_status(db, user_id or "")
    if state["state"] == "login_required":
        items.append(AttentionItem(
            "scalable-login", "warning", "Scalable session expired",
            "Log in again on the server: sudo -u scalable-cli-user -H sc login --local-read-only",
            _page("banking", "scalable"), "How to log in",
        ))
    elif state["state"] == "guard_unattested":
        items.append(AttentionItem(
            "scalable-guard", "error", "Scalable trade guard missing",
            "Sync stopped: the CLI's trade controls don't refuse every order. "
            "Set [trade_controls] allowed_isins = [] in its config.toml.",
            _page("banking", "scalable"), "Fix",
        ))
    elif state["state"] == "not_installed":
        items.append(AttentionItem(
            "scalable-install", "info", "Scalable CLI not set up",
            "Sync is on, but the sc wrapper isn't installed or sudo isn't configured for it.",
            _page("banking", "scalable"), "Set up",
        ))
    elif state["state"] == "error":
        items.append(AttentionItem(
            "scalable-failed", "warning", "Last Scalable sync failed", str(state["message"])[:300],
            _page("banking", "scalable"), "Open",
        ))
    elif state["stale"]:
        items.append(AttentionItem(
            "scalable-stale", "info", "No Scalable sync for over 3 days",
            "Holdings and cash at Scalable are only as current as the last sync.",
            "/portfolio/accounts", "Sync now",
        ))
    if state["pending_reconciliation"]:
        n = state["pending_reconciliation"]
        items.append(AttentionItem(
            "scalable-review", "warning",
            f"{n} Scalable position{'s' if n > 1 else ''} match{'' if n > 1 else 'es'} a hand-entered holding",
            "They aren't counted until you choose: replace the manual row, or keep both.",
            "/portfolio/accounts#scalable-review", "Review",
        ))


def _setting_items(settings: dict[str, Any], clock: date, items: list[AttentionItem]) -> None:
    if settings.get("tax_nv_certificate"):
        valid_until = _parse_date(settings.get("tax_nv_valid_until"))
        if valid_until is None:
            items.append(AttentionItem(
                "nv-date-missing", "info", "NV certificate has no expiry date",
                "Add it so the cockpit can warn before it runs out.", _page("tax"), "Add date",
            ))
        elif valid_until < clock:
            items.append(AttentionItem(
                "nv-expired", "error", "NV certificate has expired",
                f"It ran out on {valid_until.isoformat()}; banks withhold tax again until you file a new one.",
                _page("tax"), "Update",
            ))
        elif (valid_until - clock).days <= NV_WARN_DAYS:
            items.append(AttentionItem(
                "nv-expiring", "warning", "NV certificate expires soon",
                f"Valid until {valid_until.isoformat()} ({(valid_until - clock).days} days).",
                _page("tax"), "Update",
            ))

    if not str(settings.get("sec_edgar_user_agent") or "").strip():
        items.append(AttentionItem(
            "sec-agent", "info", "SEC EDGAR contact missing",
            "sec.gov asks automated clients for a name and e-mail; the insider refresh sends a generic one.",
            _page("market_data", "sec_edgar"), "Add contact",
        ))

    basket = [s for s in str(settings.get("alphacrafter_index_basket") or "").split(",") if s.strip()]
    if 0 < len(basket) < ALPHACRAFTER_MIN_BASKET:
        items.append(AttentionItem(
            "alphacrafter-basket", "warning", "AlphaCrafter universe is ignored",
            f"It lists {len(basket)} tickers; under {ALPHACRAFTER_MIN_BASKET} the built-in 150-name "
            "universe is used instead. Clear it or add more names.",
            _page("research"), "Review",
        ))

    for key in ("isin_ticker_overrides", "etf_index_map"):
        if not isinstance(settings.get(key), dict):
            items.append(AttentionItem(
                f"json-{key}", "error", f"{CATALOG[key].label} is corrupted",
                "The stored value is not a JSON object, so it is ignored. Re-enter or clear it.",
                _page("market_data"), "Fix",
            ))


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
