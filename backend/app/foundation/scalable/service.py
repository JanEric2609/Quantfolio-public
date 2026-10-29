"""Read-only Scalable Capital sync: sc CLI → broker tables, wealth ledger, tax ledger.

Only reads. Nothing here can place, change or cancel an order, a savings plan
or anything else at Scalable (see ``cli.py`` for the four independent guards).

A sync pulls overview, cash, holdings, savings plans, overnight balance and
the transaction history, writes them in one DB transaction, then runs the
post-steps (ticker resolution, holdings mirror, snapshot totals), each on its
own so one failing never undoes the synced data.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.foundation.models.entities import (
    ActivityLedgerEntry,
    BrokerPosition,
    BrokerSyncLog,
    ConnectedAccount,
)
from app.foundation.settings import get_public_settings, upsert_public_settings

from app.foundation.broker_status import (
    RUNNING_TIMEOUT,
    depot_account,
    number_setting,
    owner_user_id,
)
from app.foundation.broker_status import aware_utc as _aware
from app.foundation.broker_status import load_raw as _load_raw

from .cli import (
    SC_BINARY,
    ScalableCli,
    ScalableCliConfig,
    ScalableError,
    ScalableLoginRequired,
    ScalableNotInstalled,
    ScalableReadOnlyViolation,
    ScalableTransient,
    ScalableUsageError,
)
from .cli import ScalableProtocolError
from .mapping import (
    NOT_EXECUTED_STATUSES,
    SOURCE,
    TransactionRecord,
    as_dict,
    ledger_activity_type,
    map_cash,
    map_context,
    map_holdings,
    map_overnight,
    map_overnight_interest,
    map_overview,
    map_savings_plans,
    map_trade_details,
    map_transactions,
    status_unconfirmed,
)

logger = logging.getLogger(__name__)

FIRST_SYNC_MAX_PAGES = 200
TRANSACTION_PAGE_SIZE = 100
INCREMENTAL_OVERLAP = timedelta(days=7)
MAX_DETAILS_PER_RUN = 50
# Prices move between the overview and the holdings call: the holdings may
# differ from the overview's securities value by this much before it is named.
VALUATION_TOLERANCE_EUR = Decimal("1")
VALUATION_TOLERANCE_PCT = Decimal("0.02")


class ScalableDisabled(ScalableError):
    """The connection is switched off in the Control Center."""


class ScalableGuardNotAttested(ScalableError):
    """sc did not report trade controls that refuse every order."""


class ScalableBusy(ScalableError):
    """Another sync is already running."""


class ScalableOwnedByOtherUser(ScalableError):
    """The server's sc session already syncs into another app user."""


CliFactory = Callable[[ScalableCliConfig], ScalableCli]
_cli_factory: CliFactory = ScalableCli


def set_cli_factory(factory: CliFactory | None) -> None:
    """Tests swap in a CLI with a fake runner; ``None`` restores the real one."""
    global _cli_factory
    _cli_factory = factory or ScalableCli


def cli_config(settings: dict[str, Any]) -> ScalableCliConfig:
    return ScalableCliConfig(
        wrapper_path=str(settings.get("scalable_wrapper_path") or "/usr/local/libexec/quantfolio-sc-ro"),
        cli_user=str(settings.get("scalable_cli_user") or ""),
        binary_path=SC_BINARY,
        binary_sha256=str(settings.get("scalable_binary_sha256") or ""),
        timeout_seconds=int(number_setting(settings, "scalable_timeout_seconds", 60, 5, 600)),
    )


def make_cli(db: Session) -> ScalableCli:
    return _cli_factory(cli_config(get_public_settings(db)))


def guard_attested(capabilities: dict[str, Any]) -> bool:
    """True when sc's local trade controls refuse every order (``allowed_isins = []``)."""
    controls = capabilities.get("local_trade_controls") or {}
    return (
        bool(controls.get("enabled"))
        and bool(controls.get("isin_controls_active"))
        and bool(controls.get("allowed_isins_configured"))
        and controls.get("allowed_isins") == []
    )


# --- probe (Control Center "Test") -------------------------------------------


def probe(db: Session) -> dict[str, Any]:
    """Binary present → whoami → trade guard → portfolio → overview, naming the failed step."""
    settings = get_public_settings(db)
    steps: list[dict[str, Any]] = []

    def step(name: str, ok: bool, detail: str) -> None:
        steps.append({"name": name, "ok": ok, "detail": detail})

    cli = make_cli(db)
    try:
        cli.check_wrapper()
        cli.check_binary()
        step("binary", True, "Read-only wrapper installed" + (
            "; sc matches the pinned SHA-256" if cli.config.binary_sha256 else "; sc hash not pinned"))
        cli.run("whoami")
        step("login", True, "Logged in")
        caps = cli.run("capabilities")
        attested = guard_attested(caps)
        step("trade_guard", attested, "Every order is refused locally" if attested else
             "sc's trade controls do not refuse every order: set [trade_controls] allowed_isins = [] in its config.toml")
        if not attested and settings.get("scalable_require_read_only_guard", True):
            return {"ok": False, "steps": steps, "message": "Trade guard not attested"}
        portfolio_id, overview = _resolve_portfolio(cli, settings)
        if not portfolio_id:
            step("portfolio", False, "No portfolio: set the Scalable portfolio ID or run `sc broker context select` once")
            return {"ok": False, "steps": steps, "message": "No Scalable portfolio selected"}
        step("portfolio", True, f"Portfolio {portfolio_id}")
        # No amount here: the probe result is shared state any app user can read.
        step("overview", True, "Portfolio overview readable" if overview.total is not None else "Overview has no valuation yet")
        return {"ok": True, "steps": steps, "message": "Scalable Capital connection works (read-only)"}
    except ScalableError as exc:
        if exc.code in {"sudo_not_configured", "sandbox_blocks_sudo"}:
            name = "sudo"
        elif isinstance(exc, ScalableNotInstalled) or exc.code in {"wrapper_untrusted", "binary_hash_mismatch"}:
            name = "binary"
        elif isinstance(exc, ScalableLoginRequired):
            name = "login"
        else:
            name = "sc"
        step(name, False, f"{exc.code}: {exc.message}")
        return {"ok": False, "steps": steps, "message": exc.message, "code": exc.code, "hints": exc.hints}


def _resolve_portfolio(cli: ScalableCli, settings: dict[str, Any]) -> tuple[str | None, Any]:
    """The portfolio to read and its overview.

    The Control Center setting wins, then sc's saved broker context. With
    neither, sc picks the account's only portfolio itself (it refuses to
    guess between several), and the overview names the one it read.
    """
    portfolio_id = str(settings.get("scalable_portfolio_id") or "").strip() or map_context(
        cli.run("broker", "context", "show")
    )
    if portfolio_id:
        return portfolio_id, map_overview(cli.run("broker", "overview", "--portfolio-id", portfolio_id))
    overview = map_overview(cli.run("broker", "overview"))
    return overview.portfolio_id, overview


def _check_valuation(overview: Any, holdings: list[Any]) -> str | None:
    """Refuse holdings that cannot be the depot the overview values; name a small gap.

    Returns a warning for a gap beyond the tolerance (prices moved between the
    two calls, or a position sc reports without a value).
    """
    securities = overview.securities
    if securities is None or securities <= 0:
        return None
    if not holdings:
        raise ScalableProtocolError(
            "holdings_unreadable",
            f"Scalable values the depot at {securities} EUR but sc listed no holdings; nothing was written.",
        )
    total = sum((h.current_value or Decimal("0") for h in holdings), Decimal("0"))
    gap = abs(total - securities)
    if gap > max(VALUATION_TOLERANCE_EUR, securities * VALUATION_TOLERANCE_PCT):
        return (f"the holdings add up to {total.quantize(Decimal('0.01'))} EUR but Scalable values the depot "
                f"at {securities.quantize(Decimal('0.01'))} EUR")
    return None


# --- sync -------------------------------------------------------------------


def _start_log(db: Session, user_id: str, trigger: str) -> BrokerSyncLog:
    now = datetime.now(UTC)
    running = (
        db.query(BrokerSyncLog)
        # Any user's run: there is one sc session per server (see the unique index).
        .filter(BrokerSyncLog.source == SOURCE, BrokerSyncLog.state == "running")
        .all()
    )
    for row in running:
        if now - (_aware(row.started_at) or now) < RUNNING_TIMEOUT:
            raise ScalableBusy("busy", "A Scalable sync is already running")
        row.state = "error"
        row.error_code = "abandoned"
        row.message = "Sync did not finish (process restarted?)"
        row.finished_at = now
    db.flush()
    log = BrokerSyncLog(user_id=user_id, source=SOURCE, trigger=trigger, state="running", started_at=now)
    db.add(log)
    try:
        db.commit()
    except IntegrityError:
        # uq_broker_sync_logs_one_running: another request started a sync
        # between the check above and this insert.
        db.rollback()
        raise ScalableBusy("busy", "A Scalable sync is already running") from None
    return log


def _finish_log(db: Session, log: BrokerSyncLog, state: str, message: str, *, code: str | None = None,
                counts: dict[str, Any] | None = None) -> None:
    log.state = state
    log.message = message[:2000]
    log.error_code = code
    log.counts_json = json.dumps(counts or {}, default=str)
    log.finished_at = datetime.now(UTC)
    db.commit()


def sync(db: Session, user_id: str, *, trigger: str = "manual") -> dict[str, Any]:
    """Run one read-only sync. Returns the finished log as a dict."""
    settings = get_public_settings(db)
    if not settings.get("scalable_enabled"):
        raise ScalableDisabled("disabled", "Scalable sync is turned off in the Control Center")
    owner = owner_user_id(db)
    if owner is not None and owner != user_id:
        raise ScalableOwnedByOtherUser(
            "owned_by_other_user", "This server's Scalable login already syncs into another user's portfolio"
        )
    log = _start_log(db, user_id, trigger)
    counts: dict[str, Any] = {}
    try:
        cli = make_cli(db)
        cli.run("whoami")
        if settings.get("scalable_require_read_only_guard", True):
            if not guard_attested(cli.run("capabilities")):
                raise ScalableGuardNotAttested(
                    "guard_not_attested",
                    "sc's trade controls do not refuse every order; sync stopped. Set "
                    "[trade_controls] allowed_isins = [] in the CLI user's config.toml.",
                )
        portfolio_id, overview = _resolve_portfolio(cli, settings)
        if not portfolio_id:
            raise ScalableUsageError(
                "broker_context_missing",
                "No Scalable portfolio selected: set the portfolio ID in the Control Center.",
            )
        pid = ("--portfolio-id", portfolio_id)
        cash = map_cash(cli.run("broker", "cash-breakdown", *pid))
        holdings = map_holdings(cli.run("broker", "holdings", *pid))
        valuation_warning = _check_valuation(overview, holdings)
        if valuation_warning:
            counts["valuation_gap"] = valuation_warning
        plans: list[Any] | None
        try:
            plans = map_savings_plans(cli.run("broker", "savings-plans", *pid))
        except (ScalableUsageError, ScalableTransient) as exc:
            # Unavailable is not "none": the last synced plans stay as they are.
            logger.info("Scalable savings plans unavailable: %s", exc.code)
            plans = None
            counts["plans_unavailable"] = True
        try:
            overnight = map_overnight(cli.run("overnight"))
        except (ScalableUsageError, ScalableTransient) as exc:
            logger.info("Scalable overnight account unavailable: %s", exc.code)
            overnight = None

        depot = depot_account(db, user_id)
        raw = _load_raw(depot)
        watermark = raw.get("transactions_watermark")
        since = None
        if watermark:
            try:
                since = datetime.fromisoformat(watermark) - INCREMENTAL_OVERLAP
            except ValueError:
                since = None
        transactions, truncated = _fetch_transactions(cli, portfolio_id, since)
        overnight_read = False
        if overnight is not None and overnight.balance is not None:
            # Read the whole interest history until one read succeeded (a
            # wrapper from before this read refuses it while the watermark
            # moves on).
            interest = _fetch_overnight_interest(cli, since if raw.get("overnight_interest_read") else None)
            if interest is None:
                counts["overnight_interest"] = "unavailable"
            else:
                transactions += interest
                overnight_read = True
        counts["transactions_seen"] = len(transactions)
        if truncated:
            counts["transactions_truncated"] = True
        details, deferred = _fetch_trade_details(cli, db, user_id, portfolio_id, transactions)
        if deferred:
            counts["trades_deferred"] = len(deferred)

        counts.update(
            _write(db, user_id, portfolio_id, overview, cash, holdings, plans, overnight, transactions, details,
                   deferred=deferred, truncated=truncated, overnight_interest_read=overnight_read)
        )
        db.commit()
    except ScalableReadOnlyViolation as exc:
        db.rollback()
        # A read tripped a write guard: something is wrong on our side. Stop
        # syncing until a human has looked.
        upsert_public_settings(db, {"scalable_enabled": False})
        _finish_log(db, log, "error", f"sc refused a write ({exc.code}); Scalable sync disabled. {exc.message}",
                    code=exc.code)
        raise
    except ScalableLoginRequired as exc:
        db.rollback()
        _finish_log(db, log, "warning", "Scalable session expired: log in again on the server "
                    "(sudo -u scalable-cli-user -H sc login --local-read-only).", code=exc.code)
        raise
    except ScalableError as exc:
        db.rollback()
        _finish_log(db, log, "error", exc.message, code=exc.code)
        raise
    except Exception as exc:
        db.rollback()
        logger.exception("Scalable sync failed")
        _finish_log(db, log, "error", f"Unexpected error: {type(exc).__name__}", code="internal_error")
        raise

    counts.update(_post_steps(db, user_id))
    from app.foundation.portfolio_service import invalidate_wealth_cache

    invalidate_wealth_cache(user_id)
    skipped = counts.get("skipped_status") or {}
    truncated = bool(counts.get("transactions_truncated"))
    failed_steps = sorted(k for k, v in (counts.get("post_steps") or {}).items() if v == "failed")
    tax_problems = (counts.get("post_steps") or {}).get("tax_warnings") or []
    gap = counts.get("valuation_gap")
    state = "warning" if skipped or truncated or failed_steps or gap or tax_problems else "success"
    message = f"Synced {counts.get('positions', 0)} positions and {counts.get('ledger_created', 0)} new transactions"
    if skipped:
        message += "; skipped transactions with unrecognised status " + ", ".join(sorted(skipped))
    if gap:
        message += f"; {gap}"
    if failed_steps:
        message += "; these follow-up steps failed and run again next sync: " + ", ".join(failed_steps)
    if tax_problems:
        message += "; " + "; ".join(tax_problems)
    if counts.get("trades_deferred"):
        message += (f"; {counts['trades_deferred']} transactions wait for their price, fees or tax "
                    "until the next sync")
    if truncated:
        message += (f"; the transaction history is longer than {FIRST_SYNC_MAX_PAGES * TRANSACTION_PAGE_SIZE} "
                    "entries, older ones were not imported")
    _finish_log(db, log, state, message, counts=counts)
    return log_to_dict(log)


def _fetch_transactions(
    cli: ScalableCli, portfolio_id: str, since: datetime | None,
) -> tuple[list[TransactionRecord], bool]:
    """Every transaction since *since*, and whether the page cap cut the history short."""
    args = ["broker", "transactions", "--portfolio-id", portfolio_id, "--page-size", str(TRANSACTION_PAGE_SIZE)]
    if since is not None:
        args += ["--from-time", _aware(since).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")]
    out: list[TransactionRecord] = []
    seen_ids: set[str] = set()
    cursor: str | None = None
    for _page in range(FIRST_SYNC_MAX_PAGES):
        page_args = args + (["--cursor", cursor] if cursor else [])
        items, next_cursor = map_transactions(cli.run(*page_args))
        fresh = [t for t in items if t.id not in seen_ids]
        seen_ids.update(t.id for t in fresh)
        out.extend(fresh)
        if not items or not fresh or not next_cursor or next_cursor == cursor:
            return out, False
        cursor = next_cursor
    return out, True


def _fetch_overnight_interest(cli: ScalableCli, since: datetime | None) -> list[TransactionRecord] | None:
    """Interest credited to the overnight account since *since*, or None when sc can't say.

    A wrapper installed before this read was allowed refuses it; the rest of
    the sync goes on and the log names the gap.
    """
    args = ["overnight", "transactions", "--page-size", str(TRANSACTION_PAGE_SIZE), "--type-filter", "INTEREST"]
    if since is not None:
        args += ["--from-time", _aware(since).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")]
    out: list[TransactionRecord] = []
    cursor: str | None = None
    try:
        for _page in range(FIRST_SYNC_MAX_PAGES):
            page_args = args + (["--cursor", cursor] if cursor else [])
            items, next_cursor = map_overnight_interest(cli.run(*page_args))
            out.extend(items)
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
    except (ScalableUsageError, ScalableTransient, ScalableProtocolError) as exc:
        logger.info("Scalable overnight interest unavailable: %s", exc.code)
        return None
    return out


def _dedupe_hash(portfolio_id: str, tx_id: str) -> str:
    return hashlib.sha256(f"{SOURCE}:{portfolio_id}:{tx_id}".encode()).hexdigest()


# Rows whose ``transaction details`` carry what the list lacks: price, fees and
# withheld tax for trades, the gross and the withheld tax for dividends and
# interest.
_DETAIL_ACTIVITIES = frozenset({"buy", "sell", "dividend", "interest"})


def _fetch_trade_details(cli: ScalableCli, db: Session, user_id: str, portfolio_id: str,
                         transactions: list[TransactionRecord]) -> tuple[dict[str, Any], set[str]]:
    """Price, fees and tax only come from ``transaction details``; fetch them for new rows.

    Returns the details by transaction id and the ids of rows that must wait
    for a later run (over the per-run budget, or details briefly unavailable).
    Those rows are not written yet, so the next run fetches them again
    instead of booking them without price, fees or tax for good. Overnight
    interest is not a broker transaction and has no details.
    """
    hashes = {
        _dedupe_hash(portfolio_id, t.id): t for t in transactions
        if ledger_activity_type(t) in _DETAIL_ACTIVITIES and not t.id.startswith("overnight:")
    }
    if not hashes:
        return {}, set()
    known = {
        h for (h,) in db.query(ActivityLedgerEntry.dedupe_hash).filter(
            ActivityLedgerEntry.user_id == user_id,
            ActivityLedgerEntry.source == SOURCE,
            ActivityLedgerEntry.dedupe_hash.in_(list(hashes)),
        ).all()
    }
    out: dict[str, Any] = {}
    deferred: set[str] = set()
    attempted = 0
    for h, tx in hashes.items():
        if h in known:
            continue
        if attempted >= MAX_DETAILS_PER_RUN:
            deferred.add(tx.id)
            continue
        attempted += 1
        try:
            out[tx.id] = map_trade_details(
                cli.run("broker", "transaction", "details", "--portfolio-id", portfolio_id, "--transaction-id", tx.id)
            )
        except ScalableTransient as exc:
            logger.info("Scalable details briefly unavailable for a transaction: %s", exc.code)
            deferred.add(tx.id)
        except ScalableUsageError as exc:
            # sc has no details for this one (and won't next time): book it from the list row.
            logger.info("Scalable details unavailable for a transaction: %s", exc.code)
    return out, deferred


def _upsert_account(db: Session, user_id: str, external_id: str, account_type: str, name: str,
                    balance: Decimal | None, now: datetime) -> ConnectedAccount:
    account = (
        db.query(ConnectedAccount)
        .filter(
            ConnectedAccount.user_id == user_id,
            ConnectedAccount.source == SOURCE,
            ConnectedAccount.external_id == external_id,
        )
        .one_or_none()
    )
    if account is None:
        account = ConnectedAccount(
            user_id=user_id, source=SOURCE, external_id=external_id, name=name,
            institution="Scalable Capital", account_type=account_type, currency="EUR",
        )
        db.add(account)
        db.flush()
    account.account_type = account_type
    account.name = name
    if balance is not None:
        account.balance = balance
    account.last_synced = now
    return account


def _write(db: Session, user_id: str, portfolio_id: str, overview, cash, holdings, plans, overnight,
           transactions: list[TransactionRecord], details: dict[str, Any], *,
           deferred: set[str] | None = None, truncated: bool = False,
           overnight_interest_read: bool = False) -> dict[str, Any]:
    now = datetime.now(UTC)
    depot = _upsert_account(db, user_id, portfolio_id, "depot", "Scalable Capital depot",
                            overview.securities if overview.securities is not None else overview.total, now)
    raw = _load_raw(depot)
    opening_hash = _dedupe_hash(portfolio_id, "opening")
    opening_booked = db.query(ActivityLedgerEntry.id).filter(
        ActivityLedgerEntry.user_id == user_id,
        ActivityLedgerEntry.source == SOURCE,
        ActivityLedgerEntry.dedupe_hash == opening_hash,
    ).first() is not None
    # The baseline is the first sync that read the depot. Syncs before the fix
    # for sc's ``result`` wrapper read nothing but still set tracking_since, so
    # a depot with neither an opening balance nor any ledger row is baselined
    # again here.
    # Only this depot's rows count: a newly selected portfolio is baselined
    # even when another Scalable portfolio was synced before.
    has_rows = opening_booked or db.query(ActivityLedgerEntry.id).filter(
        ActivityLedgerEntry.user_id == user_id,
        ActivityLedgerEntry.source == SOURCE,
        ActivityLedgerEntry.connected_account_id == depot.id,
    ).first() is not None
    first_sync = not raw.get("baseline_at") and not has_rows
    if first_sync:
        raw["tracking_since"] = date.today().isoformat()
        raw["baseline_at"] = now.isoformat()
        raw.pop("transactions_watermark", None)
    elif not raw.get("baseline_at"):
        raw["baseline_at"] = raw.get("tracking_since") or now.isoformat()
    _upsert_account(db, user_id, f"{portfolio_id}:cash", "cash", "Scalable Capital cash", cash.cash_balance, now)
    overnight_account = None
    if overnight is not None and overnight.balance is not None:
        overnight_account = _upsert_account(db, user_id, f"{portfolio_id}:overnight", "savings",
                                            "Scalable Capital overnight", overnight.balance, now)
    # Security type per ISIN, kept after a position is sold: a sale is taxed
    # as a share or as a fund by what was sold.
    known_types = as_dict(raw.get("security_types"))
    for rec in holdings:
        if rec.security_type:
            known_types[rec.isin] = rec.security_type
    raw["security_types"] = known_types

    # Positions: upsert by ISIN, drop the ones no longer held.
    existing = {
        p.isin: p for p in db.query(BrokerPosition).filter(BrokerPosition.connected_account_id == depot.id).all()
    }
    seen: set[str] = set()
    for rec in holdings:
        seen.add(rec.isin)
        pos = existing.get(rec.isin)
        if pos is None:
            pos = BrokerPosition(user_id=user_id, connected_account_id=depot.id, source=SOURCE, isin=rec.isin,
                                 name=rec.name, quantity=rec.quantity)
            db.add(pos)
        pos.name = rec.name
        pos.security_type = rec.security_type
        pos.quantity = rec.quantity
        pos.pending_quantity = rec.pending_quantity
        pos.avg_buy_price = rec.avg_buy_price
        pos.current_price = rec.current_price
        pos.current_value = rec.current_value
        pos.currency = rec.currency
        pos.price_timestamp = rec.price_timestamp
        pos.price_outdated = rec.price_outdated
        pos.last_synced = now
    removed = 0
    for isin, pos in existing.items():
        if isin not in seen:
            db.delete(pos)
            removed += 1
    db.flush()

    tracking_since = raw.get("tracking_since") or date.today().isoformat()
    deferred = deferred or set()
    ledger = _write_ledger(db, user_id, depot, portfolio_id, [t for t in transactions if t.id not in deferred],
                           details, date.fromisoformat(tracking_since),
                           baseline_at=now if first_sync else None, overnight_account=overnight_account)

    if first_sync:
        # The first sync brings money into the tracked perimeter that earlier
        # snapshots never saw. Book it once as a contribution so time-weighted
        # return doesn't read it as a gain; flows up to now are history.
        opening = sum((h.current_value or Decimal("0") for h in holdings), Decimal("0"))
        opening += cash.cash_balance or Decimal("0")
        opening += (overnight.balance if overnight is not None and overnight.balance is not None else Decimal("0"))
        if opening:
            db.add(ActivityLedgerEntry(
                user_id=user_id, connected_account_id=depot.id, source=SOURCE,
                external_id=f"opening:{portfolio_id}", dedupe_hash=opening_hash,
                activity_type="cashflow", date=date.fromisoformat(tracking_since), amount=opening,
                currency="EUR", description="Scalable Capital opening balance (first sync)",
                review_state="trusted", raw_json=json.dumps({"opening_balance": True}),
            ))

    latest = max((t.occurred_at for t in transactions if t.occurred_at), default=None)
    raw.update({
        "portfolio_id": portfolio_id,
        "account_id": overview.account_id,
        "tracking_since": tracking_since,
        "overview": {k: (str(v) if v is not None else None) for k, v in asdict(overview).items()},
        "cash": {k: (str(v) if v is not None else None) for k, v in asdict(cash).items()},
        "overnight": ({k: (str(v) if v is not None else None) for k, v in asdict(overnight).items()}
                      if overnight is not None else None),
    })
    if overnight_interest_read:
        raw["overnight_interest_read"] = True
    if plans is not None:
        raw["savings_plans"] = [
            {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(p).items()} for p in plans
        ]
        # Only set when the plans were really fetched: a failed fetch keeps the old
        # plans and the old time, so the page can say how fresh they are.
        raw["plans_synced_at"] = datetime.now(UTC).isoformat()
    waiting = [t for t in transactions if t.id in deferred]
    if truncated or any(t.occurred_at is None for t in waiting):
        # The history was not read to its end: keep the old watermark so the
        # next run reads the same span again instead of skipping past it.
        pass
    elif waiting:
        # Re-read from the first trade still waiting for its details.
        raw["transactions_watermark"] = min(t.occurred_at for t in waiting if t.occurred_at).isoformat()
    elif latest is not None:
        previous = raw.get("transactions_watermark")
        if not previous or latest.isoformat() > previous:
            raw["transactions_watermark"] = latest.isoformat()
    depot.raw_json = json.dumps(raw, default=str)
    return {
        "positions": len(holdings), "positions_removed": removed,
        "savings_plans": len(raw.get("savings_plans") or []), **ledger,
    }


_PRE_TRACKING_TYPE = "external_pre_sync"


def _write_ledger(db: Session, user_id: str, depot: ConnectedAccount, portfolio_id: str,
                  transactions: list[TransactionRecord], details: dict[str, Any], tracking_since: date, *,
                  baseline_at: datetime | None = None,
                  overnight_account: ConnectedAccount | None = None) -> dict[str, Any]:
    """Write new rows to the wealth ledger.

    *baseline_at* is set on the baseline sync only: its opening balance
    already holds every deposit booked up to then, so those are history too.
    A deposit first seen by a later sync was booked after the snapshot.
    """
    hashes = {_dedupe_hash(portfolio_id, t.id): t for t in transactions}
    existing = {
        h for (h,) in db.query(ActivityLedgerEntry.dedupe_hash).filter(
            ActivityLedgerEntry.user_id == user_id,
            ActivityLedgerEntry.source == SOURCE,
            ActivityLedgerEntry.dedupe_hash.in_(list(hashes) or [""]),
        ).all()
    }
    created = 0
    skipped_status: dict[str, int] = {}
    for h, tx in hashes.items():
        if h in existing:
            continue
        activity = ledger_activity_type(tx)
        if activity is None:
            known = (tx.status or "").upper() in NOT_EXECUTED_STATUSES
            if not tx.is_cancellation and status_unconfirmed(tx) and not known:
                label = tx.status or "(none)"
                skipped_status[label] = skipped_status.get(label, 0) + 1
            continue
        if tx.id.startswith("overnight:") and overnight_account is None:
            continue
        when = (tx.occurred_at or datetime.now(UTC)).date()
        amount = tx.amount or Decimal("0")
        quantity = tx.quantity
        price = None
        fees = None
        tax_raw: dict[str, str] = {}
        detail = details.get(tx.id)
        if activity in {"buy", "sell"}:
            if detail is not None:
                price = detail.average_price
                fees = detail.fees
                if detail.total_amount is not None:
                    amount = detail.total_amount
                if detail.filled_quantity is not None:
                    quantity = detail.filled_quantity
            quantity = abs(quantity) if quantity is not None else None
            if price is None and quantity and amount:
                price = abs(amount) / quantity
            if detail is not None and detail.taxes is not None:
                tax_raw["tax"] = str(detail.taxes)
            # Wealth-ledger sign: cash leaving the account is negative.
            amount = -abs(amount) if activity == "buy" else abs(amount)
        elif activity == "cashflow":
            cash_type = tx.cash_type or ""
            if any(t in cash_type for t in ("WITHDRAW", "PAYOUT", "PAY_OUT")):
                amount = -abs(amount)
            elif any(t in cash_type for t in ("DEPOSIT", "PAY_IN", "PAYIN")):
                amount = abs(amount)
            pre_baseline = baseline_at is not None and tx.occurred_at is not None and tx.occurred_at <= baseline_at
            if when < tracking_since or pre_baseline:
                # Before the first sync this account was outside the tracked
                # perimeter; its deposits are history, not contributions.
                activity = _PRE_TRACKING_TYPE
        elif activity in {"dividend", "interest"} and detail is not None:
            if detail.cash_gross is not None:
                tax_raw["gross"] = str(detail.cash_gross)
            if detail.cash_tax is not None:
                tax_raw["tax"] = str(detail.cash_tax)
        account = depot
        if tx.id.startswith("overnight:"):
            if overnight_account is None:  # skipped above; narrows the type
                continue
            account = overnight_account
        db.add(ActivityLedgerEntry(
            user_id=user_id, connected_account_id=account.id, source=SOURCE, external_id=tx.id[:120],
            dedupe_hash=h, activity_type=activity, date=when, amount=amount, currency=tx.currency,
            description=(tx.description or tx.type or tx.kind)[:300], isin=tx.isin, quantity=quantity,
            price=price, fees=fees,
            review_state="needs_review" if activity == "other" else "trusted",
            raw_json=json.dumps({**tx.raw, "details": detail is not None, **tax_raw}, default=str),
        ))
        created += 1
    return {"ledger_created": created, "skipped_status": skipped_status}


def _post_steps(db: Session, user_id: str) -> dict[str, Any]:
    from app.foundation.broker_sync import post_sync_steps

    return post_sync_steps(db, user_id, tax_step=lambda: ingest_tax_events(db, user_id), label="Scalable")


def ingest_tax_events(db: Session, user_id: str) -> dict[str, Any]:
    """The Scalable ledger into the tax cockpit (``broker_sync.ingest_broker_tax_events``).

    The security type per ISIN comes from every Scalable depot the user ever
    synced; a security's type is the same in each, so their maps are merged.
    """
    from app.foundation.broker_sync import ingest_broker_tax_events

    security_types: dict[str, Any] = {}
    for account in db.query(ConnectedAccount).filter(
        ConnectedAccount.user_id == user_id,
        ConnectedAccount.source == SOURCE,
        ConnectedAccount.account_type == "depot",
    ).order_by(ConnectedAccount.created_at):
        security_types.update(as_dict(_load_raw(account).get("security_types")))
    return ingest_broker_tax_events(
        db, user_id, source=SOURCE, lot_source="scalable_sync", label="Scalable Capital",
        security_types=security_types,
    )


def log_to_dict(log: BrokerSyncLog) -> dict[str, Any]:
    try:
        counts = json.loads(log.counts_json or "{}")
    except ValueError:
        counts = {}
    return {
        "id": log.id,
        "source": log.source,
        "trigger": log.trigger,
        "state": log.state,
        "error_code": log.error_code,
        "message": log.message,
        "counts": counts,
        "started_at": _aware(log.started_at),
        "finished_at": _aware(log.finished_at),
    }


def sync_logs(db: Session, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    rows = (
        db.query(BrokerSyncLog)
        .filter(BrokerSyncLog.user_id == user_id, BrokerSyncLog.source == SOURCE)
        .order_by(BrokerSyncLog.started_at.desc())
        .limit(max(1, min(limit, 100)))
        .all()
    )
    return [log_to_dict(r) for r in rows]


def savings_plans(db: Session, user_id: str) -> list[dict[str, Any]]:
    return list(_load_raw(depot_account(db, user_id)).get("savings_plans") or [])


# --- reconciliation with hand-entered holdings -------------------------------


def reconciliation_preview(db: Session, user_id: str) -> list[dict[str, Any]]:
    """Manual holdings that match a synced Scalable position (dry-run, writes nothing)."""
    from app.foundation.broker_sync import reconciliation_preview as preview

    return preview(db, user_id, SOURCE)


def apply_reconciliation(db: Session, user_id: str, decisions: dict[str, str]) -> dict[str, int]:
    """Apply the owner's decision per manual holding id: ``replace`` or ``keep_both``."""
    from app.foundation.broker_sync import apply_reconciliation as apply

    return apply(db, user_id, SOURCE, decisions)
