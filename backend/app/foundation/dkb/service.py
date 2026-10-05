"""DKB sync orchestration service.

Extracted from the monolithic dkb.py. Manages in-memory sync sessions,
background thread execution, and persistence of snapshot data to the DB.
"""

import logging
import threading
from decimal import Decimal
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import MultipleResultsFound
from sqlalchemy.orm import Session

from app.foundation.core.config import get_settings
from app.foundation.models.entities import (
    DkbAccount,
    DkbPosition,
    DkbSyncLog,
    DkbTransaction,
    Expense,
    now_utc,
)
from app.foundation.expense_rules import apply_rules
from app.foundation.portfolio.name_resolver import resolve_position_name
from app.foundation.settings import get_public_settings, get_secret
from app.foundation.dkb.adapter import DkbFinTSAdapter
from app.foundation.dkb.models import (
    DKB_PUBLIC_TEST_PRODUCT_ID,
    DkbFinTSConnectionRejected,
    DkbFinTSManualTanRequired,
    DkbFinTSTanTimeout,
    SyncSession,
    SyncState,
    _SESSION_TTL_SECONDS,
)
from app.foundation.dkb.utils import (
    _ascii_safe,
    _safe_fints_url,
    _sanitize_fints_error,
    dkb_transaction_hash,
)

logger = logging.getLogger(__name__)


class DkbSyncService:
    """Orchestrates DKB FinTS sync sessions.

    Maintains an in-memory dict of ``SyncSession`` objects with TTL-based
    eviction. ``trigger()`` validates preconditions, creates a session,
    and spawns a daemon thread to run the actual FinTS dialog.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, SyncSession] = {}
        self._lock = threading.Lock()

    def _evict_stale(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(seconds=_SESSION_TTL_SECONDS)
        stale = [sid for sid, s in self._sessions.items() if s.updated_at < cutoff]
        for sid in stale:
            del self._sessions[sid]
        if stale:
            logger.debug("DKB session store: evicted %d stale session(s).", len(stale))

    def get(self, session_id: str) -> SyncSession | None:
        with self._lock:
            self._evict_stale()
            return self._sessions.get(session_id)

    def trigger(
        self, db: Session, user_id: str, force: bool = False,
        use_test_product_id: bool = False,
    ) -> SyncSession:
        with self._lock:
            self._evict_stale()
        public = get_public_settings(db)
        provider = str(public.get("dkb_provider") or "fints").lower()
        cached = False if force else self._cached_today(db, user_id)
        if cached:
            session = self._session("cached", "DKB data was already synced today.", provider)
            self._sessions[session.session_id] = session
            self._persist_session_logs(db, user_id, session.session_id)
            return session

        secret = get_secret(db, "dkb")
        if not secret:
            session = self._session(
                "failed", "DKB credentials are not configured. Add username and PIN in Settings first.", provider,
            )
            self._sessions[session.session_id] = session
            self._persist_session_logs(db, user_id, session.session_id)
            return session

        pin, meta = secret
        username = meta.get("username")
        if not username:
            session = self._session("failed", "DKB username is missing.", provider)
            self._sessions[session.session_id] = session
            self._persist_session_logs(db, user_id, session.session_id)
            return session

        product_id = public.get("dkb_product_id") or meta.get("product_id")
        system_id = meta.get("fints_system_id_test" if use_test_product_id else "fints_system_id")
        # Bootstrap: if fints_system_id not yet established for regular sync but a
        # successful self-test already negotiated fints_system_id_test, reuse it —
        # both paths use the same product_id so the system_id is compatible.
        if not system_id and not use_test_product_id:
            system_id = meta.get("fints_system_id_test")
        if use_test_product_id or not product_id:
            product_id = DKB_PUBLIC_TEST_PRODUCT_ID

        if provider != "fints":
            session = self._session(
                "failed", f"Unsupported DKB provider: {provider}. Only 'fints' is supported.", provider,
            )
            self._sessions[session.session_id] = session
            self._persist_session_logs(db, user_id, session.session_id)
            return session

        session = self._session(
            "pending_tan", "Sync started. Confirm the login in the DKB app if prompted.", provider,
        )

        if use_test_product_id:
            session.logs.append({
                "state": "pending_tan",
                "message": "Using community test product ID for this sync -- your own product ID is not affected.",
                "provider": provider,
                "created_at": now_utc().isoformat(),
            })

        self._sessions[session.session_id] = session
        adapter = DkbFinTSAdapter(
            _safe_fints_url(public.get("dkb_fints_url") or get_settings().dkb_fints_url),
            public.get("dkb_blz") or get_settings().dkb_blz,
            username,
            pin,
            tan_security_function=public.get("dkb_tan_security_function") or meta.get("tan_security_function"),
            tan_medium=public.get("dkb_tan_medium") or meta.get("tan_medium"),
            product_id=product_id,
            system_id=system_id,
            push_timeout_seconds=int(public.get("dkb_push_timeout_seconds") or 120),
            poll_interval_seconds=int(public.get("dkb_poll_interval_seconds") or 5),
        )
        if use_test_product_id:
            adapter.system_id_meta_key = "fints_system_id_test"
        adapter.debug_fints_logging = bool(public.get("dkb_debug_fints_logging", False))
        # Do not hand the request-scoped session to the daemon thread.
        thread = threading.Thread(
            target=self._run_sync,
            args=(session.session_id, adapter, user_id),
            daemon=True,
        )
        thread.start()
        return session

    def _session(self, state: SyncState, message: str, provider: str) -> SyncSession:
        session = SyncSession(str(uuid4()), state, message, provider=provider)
        session.logs.append({
            "state": state,
            "message": message,
            "provider": provider,
            "created_at": session.created_at.isoformat(),
        })
        return session

    def _cached_today(self, db: Session, user_id: str) -> bool:
        row = (
            db.query(DkbAccount)
            .filter(DkbAccount.user_id == user_id, DkbAccount.last_synced.is_not(None))
            .order_by(DkbAccount.last_synced.desc())
            .first()
        )
        if row is None or row.last_synced is None:
            return False
        return row.last_synced.date() == datetime.now(UTC).date()

    def _run_sync(self, session_id: str, adapter: Any, user_id: str, db: Session | None = None) -> None:
        from app.foundation.core.db import SessionLocal
        from app.foundation.settings import update_secret_meta

        try:
            try:
                snapshot = adapter.fetch_snapshot(
                    state_callback=lambda state, message, **extra: self._update(
                        session_id, state, message, **extra,
                    )
                )
                errors: list[dict[str, str]] = snapshot.pop("errors", [])
                snapshot.pop("debug_log", None)

                system_id = snapshot.pop("system_id", None)
                meta_key = getattr(adapter, "system_id_meta_key", "fints_system_id")
                if system_id and adapter.system_id != system_id:
                    persist_db = db or SessionLocal()
                    try:
                        update_secret_meta(persist_db, "dkb", {meta_key: system_id})
                    finally:
                        if db is None:
                            persist_db.close()

                with SessionLocal() as persist_db:
                    self.persist_snapshot(persist_db, user_id, snapshot)

                for err in errors:
                    self._update(
                        session_id, "confirmed",
                        f"Could not read {err['op']} for {err['account']}: {err['message']}",
                    )

                if errors:
                    error_details = "; ".join(
                        f"{e['op']} {e['account']}: {e['message']}" for e in errors
                    )
                    self._update(
                        session_id, "confirmed",
                        f"DKB sync completed with {len(errors)} issue(s): {error_details}",
                    )
                    logger.warning("DKB FinTS partial sync: %s", error_details)
                else:
                    self._update(session_id, "confirmed", "DKB sync completed.")
            except DkbFinTSConnectionRejected:
                logger.error("DKB FinTS sync rejected (HTTP 400)", exc_info=True)
                msg = (
                    "DKB rejected the FinTS connection (HTTP 400). "
                    "Verify that your username (Anmeldename), PIN, and BLZ (12030000) are correct. "
                    "If a DKB-App push arrived the credentials are accepted -- "
                    "the rejection is at a later segment, which is unusual."
                )
                self._update(session_id, "failed", msg)
            except DkbFinTSTanTimeout:
                logger.error("DKB FinTS sync timed out waiting for push TAN confirmation", exc_info=True)
                self._update(
                    session_id, "expired",
                    "Sync timed out waiting for DKB-App confirmation -- please retry and approve the push notification promptly.",
                )
            except DkbFinTSManualTanRequired as exc:
                logger.warning("DKB FinTS sync requires a manual TAN method: %s", exc)
                self._update(session_id, "needs_manual_tan", str(exc))
            except Exception as exc:
                logger.error("DKB FinTS sync failed", exc_info=True)
                raw_detail = str(exc)
                detail = _sanitize_fints_error(
                    raw_detail, getattr(adapter, "pin", None), getattr(adapter, "username", None),
                )
                labelled = f"{type(exc).__name__}: {detail}" if detail else detail
                message = (
                    f"DKB sync failed: {labelled}"
                    if labelled
                    else "Sync failed due to a FinTS connection error -- see server logs."
                )
                self._update(session_id, "failed", message)
        finally:
            debug_lines = getattr(adapter, "debug_log", None)
            if debug_lines:
                exchange = "\n".join(debug_lines)[:4000]
                self._append_log(session_id, f"FinTS exchange:\n{exchange}")
            with SessionLocal() as persist_db:
                self._persist_session_logs(persist_db, user_id, session_id)

    def _update(self, session_id: str, state: SyncState, message: str, **extra: Any) -> None:
        with self._lock:
            current = self._sessions.get(session_id)
            if current:
                current.state = state
                current.message = _ascii_safe(message)
                for key in (
                    "challenge", "challenge_html", "decoupled",
                    "available_tan_methods", "next_poll_after_seconds",
                ):
                    if key in extra:
                        setattr(current, key, extra[key])
                current.updated_at = now_utc()
                current.logs.append({
                    "state": state,
                    "message": message,
                    "provider": current.provider,
                    "created_at": current.updated_at.isoformat(),
                })

    def _append_log(self, session_id: str, message: str) -> None:
        with self._lock:
            current = self._sessions.get(session_id)
            if current:
                current.logs.append({
                    "state": current.state,
                    "message": _ascii_safe(message),
                    "provider": current.provider,
                    "created_at": now_utc().isoformat(),
                })

    def _persist_session_logs(self, db: Session, user_id: str, session_id: str) -> None:
        current = self._sessions.get(session_id)
        if current is None:
            return
        seen = {
            (row.state, row.message, row.created_at.isoformat())
            for row in db.query(DkbSyncLog).filter(DkbSyncLog.session_id == session_id).all()
        }
        for item in current.logs:
            key = (item["state"], item["message"], item["created_at"])
            if key in seen:
                continue
            db.add(
                DkbSyncLog(
                    user_id=user_id,
                    session_id=session_id,
                    provider=current.provider,
                    state=item["state"],
                    message=item["message"],
                    created_at=datetime.fromisoformat(item["created_at"]),
                )
            )
        db.commit()

    def persist_snapshot(self, db: Session, user_id: str, snapshot: dict[str, Any]) -> None:
        account_by_iban: dict[str | None, DkbAccount] = {}
        for item in snapshot.get("accounts", []):
            iban_val = item.get("iban")
            if not iban_val:
                logger.warning(
                    "DKB persist: skipping account row with no identifier (type=%s)", item.get("type")
                )
                continue
            try:
                account = (
                    db.query(DkbAccount)
                    .filter(DkbAccount.user_id == user_id, DkbAccount.iban == iban_val)
                    .one_or_none()
                )
            except MultipleResultsFound:
                logger.error("DKB sync: multiple DkbAccount rows for user_id=%s iban=%s", user_id, iban_val)
                raise
            if account is None:
                account = DkbAccount(user_id=user_id, type=item["type"], iban=iban_val)
                db.add(account)
                db.flush()
            account.balance = item["balance"]
            account.currency = item.get("currency", "EUR")
            account.last_synced = datetime.now(UTC)
            account_by_iban[account.iban] = account

        seen_tx_hashes: set[tuple[str, str]] = set()
        seen_expense_hashes: set[str] = set()
        for tx in snapshot.get("transactions", []):
            account = account_by_iban.get(tx.get("iban"))
            if account is None:
                continue
            raw_amount = tx.get("amount")
            try:
                amount = Decimal(str(raw_amount)) if raw_amount is not None else Decimal("0")
            except Exception:
                logger.warning("DKB persist: skipping transaction with uncoercible amount (raw=%s, ref=%s)", raw_amount, tx.get("reference"))
                continue
            if amount == Decimal("0"):
                logger.info("DKB persist: skipping transaction with zero amount (ref=%s)", tx.get("reference"))
                continue
            dedupe_hash = dkb_transaction_hash(tx["date"], amount, tx["reference"])
            batch_key = (str(account.id), dedupe_hash)
            if batch_key in seen_tx_hashes:
                continue
            seen_tx_hashes.add(batch_key)
            try:
                exists = (
                    db.query(DkbTransaction)
                    .filter(
                        DkbTransaction.account_id == account.id,
                        DkbTransaction.dedupe_hash == dedupe_hash,
                    )
                    .one_or_none()
                )
            except MultipleResultsFound:
                logger.error(
                    "DKB sync: multiple DkbTransaction rows for account_id=%s dedupe_hash=%s",
                    account.id, dedupe_hash[:20],
                )
                raise
            if exists:
                continue
            db.add(
                DkbTransaction(
                    account_id=account.id,
                    date=tx["date"],
                    amount=amount,
                    currency=tx.get("currency", "EUR"),
                    reference=_ascii_safe(tx["reference"]),
                    source="dkb",
                    dedupe_hash=dedupe_hash,
                )
            )
            if dedupe_hash not in seen_expense_hashes:
                seen_expense_hashes.add(dedupe_hash)
                try:
                    manual_expense = (
                        db.query(Expense)
                        .filter(Expense.user_id == user_id, Expense.dkb_dedupe_hash == dedupe_hash)
                        .one_or_none()
                    )
                except MultipleResultsFound:
                    logger.error(
                        "DKB sync: multiple Expense rows for user_id=%s dkb_dedupe_hash=%s",
                        user_id, dedupe_hash[:20],
                    )
                    raise
                if manual_expense is None:
                    db.add(
                        Expense(
                            user_id=user_id,
                            date=tx["date"],
                            amount=amount,
                            currency=tx.get("currency", "EUR"),
                            description=_ascii_safe(tx["reference"][:250]),
                            source="dkb_auto",
                            dkb_dedupe_hash=dedupe_hash,
                        )
                    )
                else:
                    manual_expense.source = "dkb_verified"

        if "positions" not in snapshot:
            logger.info("DKB snapshot missing 'positions' key — skipping position sync to preserve existing synced data")
        else:
            dkb_positions = snapshot["positions"]

            existing_tickers: dict[tuple[str, str], str | None] = {}
            for account in account_by_iban.values():
                rows = db.query(DkbPosition.ticker, DkbPosition.isin).filter(
                    DkbPosition.account_id == account.id,
                    DkbPosition.ticker.isnot(None),
                ).all()
                for ticker, isin in rows:
                    existing_tickers[(account.id, isin)] = ticker

            for account in account_by_iban.values():
                db.query(DkbPosition).filter(DkbPosition.account_id == account.id).delete()

            from collections import defaultdict
            aggregated: dict[tuple[str, str], dict] = defaultdict(lambda: {
                "quantity": Decimal("0"), "value_sum": Decimal("0"),
                "avg_buy_weighted_sum": Decimal("0"), "avg_buy_unknown": False,
                "current_price": None,
                "name": "", "isin": "", "account_id": None,
            })
            for position in dkb_positions:
                account = account_by_iban.get(position.get("iban"))
                if account is None:
                    continue
                key = (account.id, position["isin"])
                agg = aggregated[key]
                qty = Decimal(str(position.get("quantity", 0) or 0))
                price = Decimal(str(position.get("current_price", 0) or 0))
                value = Decimal(str(position.get("current_value", 0) or 0))
                avg_buy_raw = position.get("avg_buy_price")

                agg["quantity"] += qty
                agg["value_sum"] += value
                if avg_buy_raw is None:
                    agg["avg_buy_unknown"] = True
                else:
                    agg["avg_buy_weighted_sum"] += Decimal(str(avg_buy_raw)) * qty
                agg["current_price"] = price if price else agg["current_price"]
                agg["name"] = _ascii_safe(str(position.get("name", "")) or position["isin"])
                agg["isin"] = position["isin"]
                agg["account_id"] = account.id

            for key, agg in aggregated.items():
                account_id, isin = key
                qty = agg["quantity"]
                if agg["avg_buy_unknown"] or qty <= 0:
                    avg_buy = None
                else:
                    avg_buy = agg["avg_buy_weighted_sum"] / qty
                ticker = existing_tickers.get((account_id, isin))
                raw_wire_name = agg["name"]
                display_name = resolve_position_name(
                    {"isin": isin, "ticker": ticker or "", "name": raw_wire_name}, db=db,
                )
                if display_name != raw_wire_name:
                    # DkbPosition has no meta column (no migration this wave):
                    # the untouched FinTS string is preserved here, in the log.
                    logger.warning(
                        "DKB persist: replaced wire name %r with %r for ISIN %s",
                        raw_wire_name, display_name, isin,
                    )
                db.add(
                    DkbPosition(
                        account_id=account_id,
                        isin=isin,
                        ticker=ticker,
                        name=display_name,
                        quantity=qty,
                        avg_buy_price=avg_buy,
                        current_price=agg["current_price"],
                        current_value=agg["value_sum"],
                        last_synced=datetime.now(UTC),
                    )
                )

        db.commit()
        try:
            apply_rules(db, user_id)
            db.commit()
        except Exception as exc:
            logger.warning("Failed to apply expense rules after DKB sync: %s", exc)
        from app.foundation.portfolio_service import snapshot_book_positions, sync_dkb_to_wealth_ledger

        sync_dkb_to_wealth_ledger(db, user_id)
        try:
            # Today's positions at every broker, for the book's time-weighted return.
            snapshot_book_positions(db, user_id)
        except Exception as exc:
            db.rollback()
            logger.warning("Book position snapshot after DKB sync failed: %s", exc)
        try:
            # An accepted recommendation whose trade now shows in the depot.
            from app.foundation.recommendation_execution import detect_executions

            detect_executions(db, user_id)
        except Exception as exc:
            db.rollback()
            logger.warning("Execution detection after DKB sync failed: %s", exc)

        try:
            from app.foundation.etf_classification import classify_and_enrich

            etf_result = classify_and_enrich(db, user_id)
            logger.info(
                "ETF classification after DKB sync: classified=%d updated=%d errors=%d",
                etf_result.get("classified", 0),
                etf_result.get("updated", 0),
                len(etf_result.get("errors", [])),
            )
        except Exception as exc:
            logger.warning("ETF classification failed after DKB sync: %s", exc)

        try:
            from app.foundation.jobs import submit_job
            from app.foundation.price_backfill import backfill_user_prices

            def _backfill_job() -> None:
                from app.foundation.core.db import SessionLocal
                bf_db = SessionLocal()
                try:
                    result = backfill_user_prices(bf_db, user_id, days=1825)
                    logger.info("Backfill after DKB sync: %d/%d succeeded", result.get("succeeded", 0), result.get("total", 0))
                finally:
                    bf_db.close()

            submit_job(_backfill_job)
        except Exception as exc:
            logger.warning("Failed to schedule backfill after DKB sync: %s", exc)

        try:
            from app.foundation.dkb.dividend_ingestion import ingest_dkb_dividends

            ingestion = ingest_dkb_dividends(db, user_id)
            logger.info("DKB dividend ingestion after sync: %s", ingestion)
        except Exception as exc:
            logger.warning("DKB dividend ingestion failed (sync unaffected): %s", exc)


# Module-level singleton matching the old `dkb_sync_service` import path.
dkb_sync_service = DkbSyncService()
