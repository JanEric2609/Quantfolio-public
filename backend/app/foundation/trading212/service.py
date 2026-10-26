"""Read-only Trading 212 sync: REST API → broker tables, wealth ledger, tax ledger, pies.

Only reads (see ``client.py`` for the guards). A sync reads the account
summary, the positions and the three history feeds (fills, dividends, cash
transactions), writes them in one DB transaction, then reads the pies (fail
soft: a pie failure never fails the sync) and runs the shared post-steps
(``broker_sync.post_sync_steps``), each on its own.

The first sync reads the whole history (owner decision 2026-10-26); later
syncs stop at the first page holding a row already stored.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.foundation.models.entities import (
    ActivityLedgerEntry,
    BrokerPie,
    BrokerPosition,
    BrokerSyncLog,
    ConnectedAccount,
)
from app.foundation.settings import get_public_settings, get_secret, upsert_public_settings

from .client import (
    MAX_PAGES,
    PAGE_LIMIT,
    Trading212AuthError,
    Trading212Client,
    Trading212Config,
    Trading212Error,
    Trading212ReadOnlyViolation,
    Trading212ScopeError,
    Trading212Transient,
)
from .mapping import (
    LABEL,
    LOT_SOURCE,
    SOURCE,
    AccountSummary,
    CashRecord,
    DividendRecord,
    FillRecord,
    PositionRecord,
    cash_activity_type,
    map_dividends,
    map_instrument_types,
    map_orders,
    map_pie,
    map_pie_list,
    map_positions,
    map_summary,
    map_transactions,
    when,
)
from app.foundation.trading212_status import RUNNING_TIMEOUT, aware, depot_account, load_raw, owner_user_id

logger = logging.getLogger(__name__)

SUMMARY = "/equity/account/summary"
POSITIONS = "/equity/positions"
ORDERS = "/equity/history/orders"
DIVIDENDS = "/equity/history/dividends"
TRANSACTIONS = "/equity/history/transactions"
PIES = "/equity/pies"
INSTRUMENTS = "/equity/metadata/instruments"
FEEDS = ("orders", "dividends", "transactions")
INTEREST_WINDOW_DAYS = 30
_PRE_TRACKING_TYPE = "external_pre_sync"


class Trading212Disabled(Trading212Error):
    """The connection is switched off in the Control Center."""


class Trading212NotConfigured(Trading212Error):
    """No API key and secret are saved."""


class Trading212Busy(Trading212Error):
    """Another sync is already running."""


class Trading212OwnedByOtherUser(Trading212Error):
    """The saved key already syncs into another app user."""


class Trading212AccountChanged(Trading212Error):
    """The key now reads a different Trading 212 account than the one already synced."""


ClientFactory = Callable[[Trading212Config], Trading212Client]
_client_factory: ClientFactory = Trading212Client


def set_client_factory(factory: ClientFactory | None) -> None:
    """Tests swap in a client with a fake transport; ``None`` restores the real one."""
    global _client_factory
    _client_factory = factory or Trading212Client


def make_client(db: Session) -> Trading212Client:
    parts = get_secret(db, SOURCE)
    if not parts or not parts[0] or not parts[1].get("api_secret"):
        raise Trading212NotConfigured(
            "not_configured", "Save the Trading 212 API key and secret in the Control Center first")
    key, meta = parts
    environment = str(get_public_settings(db).get("trading212_environment") or "live")
    return _client_factory(Trading212Config(api_key=key, api_secret=str(meta["api_secret"]),
                                            environment=environment if environment in {"live", "demo"} else "live"))


# --- probe (Control Center "Test connection") --------------------------------


def probe(db: Session) -> dict[str, Any]:
    """Account → positions → history → pies, naming the step and permission that failed.

    Writes nothing and reports no amounts (the result is shared state).
    """
    steps: list[dict[str, Any]] = []

    def step(name: str, ok: bool, detail: str) -> None:
        steps.append({"name": name, "ok": ok, "detail": detail})

    checks = (
        ("account", SUMMARY, None, "Account data readable", "Account data"),
        ("portfolio", POSITIONS, None, "Positions readable", "Portfolio"),
        ("history", ORDERS, {"limit": 1}, "Order, dividend and cash history readable", "History"),
    )
    try:
        client = make_client(db)
        for name, path, params, ok_text, permission in checks:
            try:
                client.get(path, params)
                step(name, True, ok_text)
            except Trading212ScopeError:
                step(name, False, f"The key lacks the {permission} permission (or this server's IP is not allowed)")
                return {"ok": False, "steps": steps, "message": f"Enable {permission} on the Trading 212 API key"}
        try:
            client.get(PIES)
            step("pies", True, "Pies readable")
        except Trading212Error as exc:
            # Optional and deprecated upstream: the sync works without it.
            step("pies", True, f"Pies not readable ({exc.code}); the sync goes on without them")
        return {"ok": True, "steps": steps, "message": "Trading 212 connection works (read-only)"}
    except Trading212Error as exc:
        step("key" if isinstance(exc, (Trading212AuthError, Trading212NotConfigured)) else "api", False,
             f"{exc.code}: {exc.message}")
        return {"ok": False, "steps": steps, "message": exc.message, "code": exc.code}


# --- sync -------------------------------------------------------------------


def _start_log(db: Session, user_id: str, trigger: str) -> BrokerSyncLog:
    now = datetime.now(UTC)
    for row in db.query(BrokerSyncLog).filter(BrokerSyncLog.source == SOURCE, BrokerSyncLog.state == "running"):
        if now - (aware(row.started_at) or now) < RUNNING_TIMEOUT:
            raise Trading212Busy("busy", "A Trading 212 sync is already running")
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
        db.rollback()
        raise Trading212Busy("busy", "A Trading 212 sync is already running") from None
    return log


def _finish_log(db: Session, log: BrokerSyncLog, state: str, message: str, *, code: str | None = None,
                counts: dict[str, Any] | None = None) -> None:
    log.state = state
    log.message = message[:2000]
    log.error_code = code
    log.counts_json = json.dumps(counts or {}, default=str)
    log.finished_at = datetime.now(UTC)
    db.commit()


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def dedupe_hash(account_id: str, kind: str, ref: str) -> str:
    return hashlib.sha256(f"{SOURCE}:{account_id}:{kind}:{ref}".encode()).hexdigest()


def _known_hashes(db: Session, user_id: str) -> set[str]:
    return {
        h for (h,) in db.query(ActivityLedgerEntry.dedupe_hash).filter(
            ActivityLedgerEntry.user_id == user_id, ActivityLedgerEntry.source == SOURCE,
        )
    }


def sync(db: Session, user_id: str, *, trigger: str = "manual") -> dict[str, Any]:
    """Run one read-only sync. Returns the finished log as a dict."""
    settings = get_public_settings(db)
    if not settings.get("trading212_enabled"):
        raise Trading212Disabled("disabled", "Trading 212 sync is turned off in the Control Center")
    owner = owner_user_id(db)
    if owner is not None and owner != user_id:
        raise Trading212OwnedByOtherUser(
            "owned_by_other_user", "The saved Trading 212 key already syncs into another user's portfolio")
    client = make_client(db)
    log = _start_log(db, user_id, trigger)
    counts: dict[str, Any] = {}
    try:
        summary = map_summary(client.get(SUMMARY))
        account_id = summary.account_id or "default"
        depot = depot_account(db, user_id)
        if depot is not None and depot.external_id and depot.external_id != account_id:
            # Never mix two accounts (a demo key, then the live one): their
            # positions, cash and tax lots would all count.
            raise Trading212AccountChanged(
                "account_changed",
                f"The key reads Trading 212 account {account_id}, but account {depot.external_id} is already "
                "synced. Save the key of that account, or remove the old Trading 212 data before syncing another.",
            )
        positions = map_positions(client.get(POSITIONS))
        raw = load_raw(depot)
        complete = _obj(raw.get("history_complete"))
        known = _known_hashes(db, user_id)

        def stopper(feed: str, kind: str, ref: Callable[[dict[str, Any]], Any],
                    at: Callable[[dict[str, Any]], Any]) -> Callable[[list[dict[str, Any]]], bool]:
            """Stop at a page holding a stored row, once the feed was read to its end.

            Only on a page that runs newest to oldest: were a feed ever served
            oldest first, its stored rows would come before the new ones.
            """
            def stop(page: list[dict[str, Any]]) -> bool:
                if not complete.get(feed):
                    return False
                times = [t for t in (when(at(i)) for i in page) if t is not None]
                if times and times != sorted(times, reverse=True):
                    return False
                return any(ref(i) is not None and dedupe_hash(account_id, kind, str(ref(i))) in known for i in page)
            return stop

        order_items, orders_cut = client.paginate(ORDERS, stop=stopper(
            "orders", "fill", lambda i: _obj(i.get("fill")).get("id"), lambda i: _obj(i.get("fill")).get("filledAt")))
        dividend_items, dividends_cut = client.paginate(DIVIDENDS, stop=stopper(
            "dividends", "dividend", lambda i: i.get("reference"), lambda i: i.get("paidOn")))
        transaction_items, transactions_cut = client.paginate(TRANSACTIONS, stop=stopper(
            "transactions", "cash", lambda i: i.get("reference"), lambda i: i.get("dateTime")))
        fills = map_orders(order_items)
        dividends = map_dividends(dividend_items)
        cash = map_transactions(transaction_items)
        cut = {"orders": orders_cut, "dividends": dividends_cut, "transactions": transactions_cut}
        if any(cut.values()):
            counts["history_truncated"] = sorted(k for k, v in cut.items() if v)

        types: dict[str, Any] = _obj(raw.get("security_types"))
        wanted = ({p.isin for p in positions} | {f.isin for f in fills if f.isin}) - set(types)
        if wanted:
            try:
                types = {**types, **map_instrument_types(client.get(INSTRUMENTS), wanted)}
            except Trading212Transient as exc:
                logger.info("Trading 212 instrument list unavailable: %s", exc.code)
                counts["instrument_types"] = "unavailable"

        # A feed past the page cap is marked read as well: the next sync reads
        # only what is new, and the log names the history as capped.
        counts.update(_write(db, user_id, account_id, summary, positions, fills, dividends, cash,
                             security_types=types, complete={k: True for k in cut}))
        db.commit()
    except Trading212ReadOnlyViolation as exc:
        db.rollback()
        # A read asked for a path outside the allow-list: a bug on our side.
        # Stop syncing until a human has looked.
        upsert_public_settings(db, {"trading212_enabled": False})
        _finish_log(db, log, "error", f"Refused a non-read request ({exc.code}); Trading 212 sync disabled.",
                    code=exc.code)
        raise
    except (Trading212AuthError, Trading212ScopeError) as exc:
        db.rollback()
        _finish_log(db, log, "warning", exc.message, code=exc.code)
        raise
    except Trading212Error as exc:
        db.rollback()
        _finish_log(db, log, "error", exc.message, code=exc.code)
        raise
    except Exception as exc:
        db.rollback()
        logger.exception("Trading 212 sync failed")
        _finish_log(db, log, "error", f"Unexpected error: {type(exc).__name__}", code="internal_error")
        raise

    counts.update(sync_pies(db, user_id, client, positions))
    from app.foundation.broker_sync import post_sync_steps
    from app.foundation.portfolio_service import invalidate_wealth_cache

    counts.update(post_sync_steps(db, user_id, tax_step=lambda: ingest_tax_events(db, user_id), label=LABEL))
    invalidate_wealth_cache(user_id)

    post = counts.get("post_steps") or {}
    failed_steps = sorted(k for k, v in post.items() if v == "failed")
    tax_problems = post.get("tax_warnings") or []
    truncated = counts.get("history_truncated")
    corporate = counts.get("corporate_actions") or 0
    message = (f"Synced {counts.get('positions', 0)} positions and {counts.get('ledger_created', 0)} "
               "new transactions")
    if counts.get("pies_unavailable"):
        message += "; pies not readable this time, the last read stays"
    if failed_steps:
        message += "; these follow-up steps failed and run again next sync: " + ", ".join(failed_steps)
    if tax_problems:
        message += "; " + "; ".join(tax_problems)
    if corporate:
        message += (f"; {corporate} share movements without a trade (splits, spin-offs) are in the ledger "
                    "for review and not in the tax lots")
    if truncated:
        message += (f"; only the newest {MAX_PAGES * PAGE_LIMIT} rows of " + ", ".join(truncated)
                    + " were read, older history is not imported")
    state = "warning" if failed_steps or tax_problems or truncated else "success"
    _finish_log(db, log, state, message, counts=counts)
    return log_to_dict(log)


def _upsert_account(db: Session, user_id: str, external_id: str, account_type: str, name: str,
                    balance: Decimal | None, currency: str, now: datetime) -> ConnectedAccount:
    account = (
        db.query(ConnectedAccount)
        .filter(ConnectedAccount.user_id == user_id, ConnectedAccount.source == SOURCE,
                ConnectedAccount.external_id == external_id)
        .one_or_none()
    )
    if account is None:
        account = ConnectedAccount(user_id=user_id, source=SOURCE, external_id=external_id, name=name,
                                   institution=LABEL, account_type=account_type, currency=currency)
        db.add(account)
        db.flush()
    account.account_type = account_type
    account.name = name
    if balance is not None:
        account.balance = balance
    account.last_synced = now
    return account


def _per_unit(total: Decimal | None, quantity: Decimal) -> Decimal | None:
    return (total / quantity) if total is not None and quantity else None


def _write(db: Session, user_id: str, account_id: str, summary: AccountSummary, positions: list[PositionRecord],
           fills: list[FillRecord], dividends: list[DividendRecord], cash: list[CashRecord], *,
           security_types: dict[str, Any], complete: dict[str, bool]) -> dict[str, Any]:
    now = datetime.now(UTC)
    securities = sum((p.current_value or Decimal("0") for p in positions), Decimal("0"))
    depot = _upsert_account(db, user_id, account_id, "depot", "Trading 212 depot", securities,
                            summary.currency, now)
    raw = load_raw(depot)
    first_sync = not raw.get("baseline_at")
    if first_sync:
        raw["tracking_since"] = date.today().isoformat()
        raw["baseline_at"] = now.isoformat()
    # Cash held in pies is uninvested cash too: it counts with the cash account.
    free = summary.free_cash
    balance = None if free is None else free + (summary.in_pies or Decimal("0"))
    cash_account = _upsert_account(db, user_id, f"{account_id}:cash", "cash", "Trading 212 cash", balance,
                                   summary.currency, now)

    # Positions: upsert by ISIN, drop the ones no longer held. Prices and cost
    # are stored per unit in EUR (the account currency) from T212's own
    # valuation, as every real-book reader expects EUR.
    existing = {
        p.isin: p for p in db.query(BrokerPosition).filter(BrokerPosition.connected_account_id == depot.id)
    }
    seen: set[str] = set()
    instrument_currency: dict[str, str] = {}
    for rec in positions:
        if rec.isin in seen:
            continue
        seen.add(rec.isin)
        instrument_currency[rec.isin] = rec.currency
        pos = existing.get(rec.isin)
        if pos is None:
            pos = BrokerPosition(user_id=user_id, connected_account_id=depot.id, source=SOURCE, isin=rec.isin,
                                 name=rec.name, quantity=rec.quantity)
            db.add(pos)
        pos.name = rec.name
        pos.security_type = str(security_types.get(rec.isin) or "")[:32] or None
        pos.quantity = rec.quantity
        pos.pending_quantity = None
        pos.avg_buy_price = _per_unit(rec.total_cost, rec.quantity) or (
            rec.avg_buy_price if rec.currency == summary.currency else None)
        pos.current_price = _per_unit(rec.current_value, rec.quantity) or (
            rec.current_price if rec.currency == summary.currency else None)
        pos.current_value = rec.current_value
        pos.currency = summary.currency
        pos.price_timestamp = now
        pos.price_outdated = False
        pos.last_synced = now
    removed = 0
    for isin, pos in existing.items():
        if isin not in seen:
            db.delete(pos)
            removed += 1
    db.flush()

    ledger = _write_ledger(db, user_id, account_id, depot, cash_account, fills, dividends, cash,
                           baseline_at=now if first_sync else None)

    if first_sync:
        # Money already in the account before the first sync enters the
        # tracked perimeter once, as a contribution, so time-weighted return
        # doesn't read it as a gain; flows before it are history.
        opening = summary.total_value
        if opening is None:
            opening = securities + (balance or Decimal("0"))
        if opening:
            db.add(ActivityLedgerEntry(
                user_id=user_id, connected_account_id=depot.id, source=SOURCE,
                external_id=f"opening:{account_id}", dedupe_hash=dedupe_hash(account_id, "opening", "1"),
                activity_type="cashflow", date=date.today(), amount=opening, currency=summary.currency,
                description="Trading 212 opening balance (first sync)", review_state="trusted",
                raw_json=json.dumps({"opening_balance": True}),
            ))

    db.flush()
    interest = _recent_interest(db, user_id, cash_account)
    rate = (interest * Decimal(365) / Decimal(INTEREST_WINDOW_DAYS) / balance) if balance and interest else None
    history_complete = dict(raw.get("history_complete") or {})
    for feed, done in complete.items():
        history_complete[feed] = bool(history_complete.get(feed) or done)
    raw.update({
        "account_id": account_id,
        "currency": summary.currency,
        "summary": {k: (str(v) if isinstance(v, Decimal) else v) for k, v in summary.__dict__.items()},
        "security_types": {k: v for k, v in security_types.items() if isinstance(k, str)},
        "instrument_currency": {**dict(raw.get("instrument_currency") or {}), **instrument_currency},
        "quantity_in_pies": {p.isin: str(p.quantity_in_pies) for p in positions if p.quantity_in_pies},
        "history_complete": history_complete,
        "interest_30d_eur": str(interest.quantize(Decimal("0.01"))),
        "interest_rate_measured": str(rate.quantize(Decimal("0.0001"))) if rate is not None else None,
    })
    depot.raw_json = json.dumps(raw, default=str)
    cash_account.raw_json = json.dumps({
        "interest_30d_eur": raw["interest_30d_eur"], "interest_rate_measured": raw["interest_rate_measured"],
    })
    return {"positions": len(seen), "positions_removed": removed, **ledger}


def _recent_interest(db: Session, user_id: str, cash_account: ConnectedAccount) -> Decimal:
    since = date.today() - timedelta(days=INTEREST_WINDOW_DAYS)
    rows = db.query(ActivityLedgerEntry.amount).filter(
        ActivityLedgerEntry.user_id == user_id,
        ActivityLedgerEntry.source == SOURCE,
        ActivityLedgerEntry.connected_account_id == cash_account.id,
        ActivityLedgerEntry.activity_type == "interest",
        ActivityLedgerEntry.date > since,
    )
    return sum((Decimal(str(a or 0)) for (a,) in rows), Decimal("0"))


def _gross_eur(db: Session, rec: DividendRecord) -> Decimal | None:
    """The dividend before any withholding, in EUR (ECB rate of the pay day), or None."""
    gross = rec.gross_native
    if gross is None or gross <= 0:
        return None
    if rec.ticker_currency == "EUR":
        return gross
    from app.foundation.ecb_fx import eur_per_unit

    day = (rec.paid_on or datetime.now(UTC)).date()
    rate, _source = eur_per_unit(db, rec.ticker_currency, day)
    return gross * Decimal(str(rate)) if rate else None


def _write_ledger(db: Session, user_id: str, account_id: str, depot: ConnectedAccount,
                  cash_account: ConnectedAccount, fills: list[FillRecord], dividends: list[DividendRecord],
                  cash: list[CashRecord], *, baseline_at: datetime | None) -> dict[str, Any]:
    """New rows into the wealth ledger, deduped on ``trading212:<account>:<kind>:<id>``.

    Amounts are in EUR, signed the ledger's way (cash leaving is negative).
    *baseline_at* is set on the first sync only: deposits up to it are
    already in the opening balance, so they are history (``external_pre_sync``).
    """
    known = _known_hashes(db, user_id)
    created = 0
    corporate = 0
    unverified = 0

    def add(kind: str, ref: str, account: ConnectedAccount, **fields: Any) -> bool:
        h = dedupe_hash(account_id, kind, ref)
        if h in known:
            return False
        known.add(h)
        db.add(ActivityLedgerEntry(user_id=user_id, connected_account_id=account.id, source=SOURCE,
                                   external_id=f"{kind}:{ref}"[:120], dedupe_hash=h, **fields))
        return True

    for f in fills:
        day = (f.filled_at or datetime.now(UTC)).date()
        if not f.is_trade:
            corporate += add("fill", f.id, depot, activity_type="corporate_action", date=day,
                             amount=Decimal("0"), currency=f.account_currency,
                             description=f"{f.fill_type.replace('_', ' ').lower()}: {f.name}"[:300],
                             isin=f.isin, quantity=f.quantity if f.side == "BUY" else -f.quantity,
                             review_state="needs_review", raw_json=json.dumps(f.raw, default=str))
            continue
        value = f.net_value
        price = f.price_in_account_currency()
        if value is None and price is not None:
            value = price * f.quantity
        amount = -(value or Decimal("0")) if f.side == "BUY" else (value or Decimal("0"))
        created += add("fill", f.id, depot, activity_type="buy" if f.side == "BUY" else "sell", date=day,
                       amount=amount, currency=f.account_currency, description=f.name or (f.isin or ""),
                       isin=f.isin, quantity=f.quantity, price=price, fees=f.fees or None,
                       review_state="trusted" if f.isin else "needs_review",
                       raw_json=json.dumps(f.raw, default=str))

    for d in dividends:
        day = (d.paid_on or datetime.now(UTC)).date()
        gross = _gross_eur(db, d)
        # Whether T212's credit is before or after US withholding is checked
        # against the app on the first real sync (docs/trading212.md).
        extra: dict[str, Any] = {**d.raw, "withholding_unverified": True}
        if gross is not None and gross >= d.amount_eur:
            extra["gross"] = str(gross)
        if add("dividend", d.reference, depot, activity_type="dividend", date=day, amount=d.amount_eur,
               currency="EUR", description=f"Dividend {d.name}"[:300], isin=d.isin, quantity=d.quantity,
               review_state="trusted", raw_json=json.dumps(extra, default=str)):
            created += 1
            unverified += 1

    for c in cash:
        day = (c.occurred_at or datetime.now(UTC)).date()
        activity = cash_activity_type(c.kind)
        amount = c.amount
        if c.kind == "WITHDRAW" or c.kind == "FEE":
            amount = -abs(amount)
        elif c.kind == "DEPOSIT" or activity == "interest":
            amount = abs(amount)
        if activity == "cashflow" and baseline_at is not None and c.occurred_at is not None \
                and c.occurred_at <= baseline_at:
            activity = _PRE_TRACKING_TYPE
        created += add("cash", c.reference, cash_account, activity_type=activity, date=day, amount=amount,
                       currency=c.currency, description=c.kind.replace("_", " ").capitalize()[:300],
                       review_state="needs_review" if activity == "other" else "trusted",
                       raw_json=json.dumps(c.raw, default=str))
    return {"ledger_created": created + corporate, "corporate_actions": corporate,
            "dividends_unverified": unverified}


def ingest_tax_events(db: Session, user_id: str) -> dict[str, Any]:
    """The Trading 212 ledger into the tax cockpit (``broker_sync.ingest_broker_tax_events``)."""
    from app.foundation.broker_sync import ingest_broker_tax_events

    types: dict[str, Any] = {}
    for account in db.query(ConnectedAccount).filter(
        ConnectedAccount.user_id == user_id, ConnectedAccount.source == SOURCE,
        ConnectedAccount.account_type == "depot",
    ).order_by(ConnectedAccount.created_at):
        stored = load_raw(account).get("security_types")
        if isinstance(stored, dict):
            types.update(stored)
    return ingest_broker_tax_events(db, user_id, source=SOURCE, lot_source=LOT_SOURCE, label=LABEL,
                                    security_types=types)


# --- pies (fail soft) --------------------------------------------------------


def sync_pies(db: Session, user_id: str, client: Trading212Client,
              positions: list[PositionRecord]) -> dict[str, Any]:
    """Replace the stored pies with a fresh read; on any failure keep the last good one.

    T212 marks the pie endpoints deprecated, so a failure here is expected
    one day: it is recorded (``pies_unavailable``) and never fails the sync.
    """
    depot = depot_account(db, user_id)
    raw = load_raw(depot)
    by_ticker = {p.t212_ticker: p for p in positions if p.t212_ticker}
    try:
        summaries = map_pie_list(client.get(PIES))
        pies = []
        for row in summaries:
            try:
                detail = client.get(f"/equity/pies/{int(row['id'])}")
            except Trading212Transient:
                detail = None  # the list row still names the pie and its value
            pies.append(map_pie(row, detail, by_ticker))
    except Trading212ReadOnlyViolation:
        raise
    except (Trading212Error, ValueError, TypeError) as exc:
        code = str(getattr(exc, "code", "")) or "unreadable"
        logger.info("Trading 212 pies unavailable: %s", code)
        if depot is not None:
            raw.setdefault("pies_failing_since", datetime.now(UTC).isoformat())
            raw["pies_error"] = code
            depot.raw_json = json.dumps(raw, default=str)
            db.commit()
        return {"pies_unavailable": code}

    now = datetime.now(UTC)
    db.query(BrokerPie).filter(BrokerPie.user_id == user_id, BrokerPie.source == SOURCE).delete()
    for pie in pies:
        db.add(BrokerPie(
            user_id=user_id, connected_account_id=depot.id if depot else None, source=SOURCE,
            external_id=pie.id, name=pie.name, status=pie.status, cash=pie.cash, invested=pie.invested,
            value=pie.value, result=pie.result, result_pct=pie.result_pct,
            dividends_gained=pie.dividends_gained, dividends_reinvested=pie.dividends_reinvested,
            dividends_in_cash=pie.dividends_in_cash, dividend_action=pie.dividend_action, goal=pie.goal,
            end_date=pie.end_date, instruments_json=json.dumps(pie.instruments, default=str), synced_at=now,
        ))
    if depot is not None:
        raw.pop("pies_failing_since", None)
        raw.pop("pies_error", None)
        raw["pies_synced_at"] = now.isoformat()
        depot.raw_json = json.dumps(raw, default=str)
    db.commit()
    return {"pies": len(pies)}


def pies(db: Session, user_id: str) -> list[dict[str, Any]]:
    out = []
    for p in db.query(BrokerPie).filter(BrokerPie.user_id == user_id, BrokerPie.source == SOURCE).order_by(
            BrokerPie.name):
        try:
            instruments = json.loads(p.instruments_json or "[]")
        except ValueError:
            instruments = []
        out.append({
            "id": p.external_id, "name": p.name, "status": p.status,
            "cash": p.cash, "invested": p.invested, "value": p.value, "result": p.result,
            "result_pct": p.result_pct, "dividends_gained": p.dividends_gained,
            "dividends_reinvested": p.dividends_reinvested, "dividends_in_cash": p.dividends_in_cash,
            "dividend_action": p.dividend_action, "goal": p.goal,
            "end_date": p.end_date.isoformat() if p.end_date else None,
            "instruments": instruments if isinstance(instruments, list) else [],
            "synced_at": aware(p.synced_at),
        })
    return out


def log_to_dict(log: BrokerSyncLog) -> dict[str, Any]:
    try:
        counts = json.loads(log.counts_json or "{}")
    except ValueError:
        counts = {}
    return {
        "id": log.id, "source": log.source, "trigger": log.trigger, "state": log.state,
        "error_code": log.error_code, "message": log.message, "counts": counts,
        "started_at": aware(log.started_at), "finished_at": aware(log.finished_at),
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
