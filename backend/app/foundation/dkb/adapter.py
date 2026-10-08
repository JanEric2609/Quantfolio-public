"""DKB FinTS protocol adapter.

Extracted from the monolithic dkb.py. Handles all FinTS wire-level
interactions: TAN mechanism discovery, decoupled push TAN lifecycle,
account/balance/transaction/holding reads.

No DB access and no orchestration -- returns structured dicts that the
caller (DkbSyncService) persists.
"""

import logging
import re
import threading
import time
import warnings
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from collections.abc import Generator
from typing import Any

from app.foundation.dkb.models import (
    DkbFinTSConnectionRejected,
    DkbFinTSManualTanRequired,
    DkbFinTSTanTimeout,
    _PENDING_TAN_SIGNALS,
)
from app.foundation.dkb.utils import (
    _capture_dkb_logs,
    _coerce_int,
    _sanitize_fints_error,
)

logger = logging.getLogger(__name__)


def _format_parser_cause_chain(exc: BaseException, pin: str | None, username: str | None) -> str:
    """Flatten a FinTSParserError ``__cause__`` chain into one sanitized line.

    ``FinTSParserError`` subclasses ``ValueError``, so python-fints re-wraps the
    precise failing sub-element (e.g. ``Balance2.date``) as the opaque outer
    ``HISAL7.balance_booked`` and only the outer message survives ``robust_mode``.
    Walk the ``__cause__``/``__context__`` links so the exact element and the raw
    ``ValueError`` are both visible; credentials are scrubbed from every hop.
    """
    parts: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        parts.append(
            _sanitize_fints_error(f"{type(cur).__name__}: {cur}", pin, username)
        )
        cur = cur.__cause__ or cur.__context__
    return " <- ".join(parts)


@contextmanager
def _capture_parser_cause_chains(
    sink: list[str], pin: str | None, username: str | None
) -> Generator[None, None, None]:
    """Record the parser ``__cause__`` chain that ``robust_mode`` discards.

    In ``robust_mode`` python-fints downgrades a failed segment to a str-only
    ``FinTSParserWarning`` and returns a bare generic object, hiding which
    sub-element DKB actually changed. Wrap ``parse_segment`` to append the full
    chain for any failing segment while leaving behaviour identical: successful
    segments hit the original path untouched, and failing ones still get the
    original warning + generic fallback after we record the chain.
    """
    try:
        import fints.parser as _fp
    except Exception:
        yield
        return

    orig = _fp.FinTS3Parser.parse_segment

    def _patched(self: Any, segment: Any) -> Any:
        clazz = _fp.FinTS3Segment.find_subclass(segment)
        try:
            return self._parse_segment_as_class(clazz, segment)
        except _fp.FinTSParserError as exc:
            if not _fp.robust_mode:
                raise
            chain = _format_parser_cause_chain(exc, pin, username)
            if chain:
                sink.append(chain)
            # Defer to the original for the identical warning + generic fallback.
            return orig(self, segment)

    _fp.FinTS3Parser.parse_segment = _patched  # type: ignore[method-assign]
    try:
        yield
    finally:
        _fp.FinTS3Parser.parse_segment = orig  # type: ignore[method-assign]


def _patch_fints_balance2_optional_date() -> None:
    """Tolerate DKB's non-compliant HISAL balance responses.

    Per FinTS the ``date`` element of the ``Balance2`` DEG (``Saldo v2``) is
    mandatory, but DKB truncates its ``balance_booked``/``balance_pending``
    balances after the amount, omitting ``date``. python-fints then hits
    ``StopIteration`` on the required field (parser.py: "Required field
    Balance2.date was not present"), raises ``FinTSParserError``, and under
    ``robust_mode`` drops the whole HISAL7 to a generic object with no
    ``balance_booked`` attribute -- surfacing to the caller as
    ``'FinTS3Segment' object has no attribute 'balance_booked'``.

    Relaxing ``Balance2.date`` to optional makes the parser ``break`` gracefully
    (date/time = ``None``) instead of discarding the segment. This only ever
    relaxes parsing: a compliant bank still sends ``date`` and it parses exactly
    as before. The field object is shared class-wide, so one idempotent mutation
    covers every parse.
    """
    try:
        from fints.formals import Balance2
    except Exception:
        return
    fields: dict[str, Any] = getattr(Balance2, "_fields", {})
    date_field = fields.get("date")
    if date_field is not None:
        date_field.required = False


# Applied at import so every FinTS balance parse tolerates DKB's missing date.
_patch_fints_balance2_optional_date()


def _statement_field(statement: Any, key: str, default: Any = None) -> Any:
    """Read a field from an mt940 Transaction.

    python-fints returns mt940 ``Transaction`` objects whose fields live in
    the ``.data`` dict, not as attributes — plain ``getattr`` silently yields
    the default for every real transaction.
    """
    data = getattr(statement, "data", None)
    if isinstance(data, dict) and data.get(key) is not None:
        return data[key]
    value = getattr(statement, key, None)
    return default if value is None else value


def _parse_statement(statement: Any, iban: str | None) -> dict[str, Any]:
    """Convert one mt940 Transaction into the snapshot transaction dict."""
    amount_obj = _statement_field(statement, "amount")
    raw_amount = getattr(amount_obj, "amount", amount_obj)
    tx_amount = Decimal(str(raw_amount if raw_amount is not None else 0))
    tx_currency = str(getattr(amount_obj, "currency", None) or "EUR")
    reference = str(
        _statement_field(statement, "purpose", "")
        or _statement_field(statement, "applicant_name", "")
        or _statement_field(statement, "posting_text", "")
    )
    tx_date = _statement_field(statement, "date", datetime.now(UTC).date())
    return {
        "iban": iban,
        "date": tx_date,
        "amount": tx_amount,
        "currency": tx_currency,
        "reference": reference,
    }

# python-fints reads the acquisition price of a depot position from one MT535
# clause, ':70E::HOLD//<n>STK|2<int>,<frac>+<CCY>'. Several German banks send
# extra qualifiers between STK and the line break (':70E::HOLD//1STK++++20231231+'
# then '2123,45+EUR', i.e. '1STK++++20231231+|2123,45+EUR' once python-fints
# joins the lines), which the library regex rejects, so ``acquisitionprice`` came
# back None. This regex accepts the original form and anything between STK and '|2'.
_TOLERANT_ACQUISITION_RE = re.compile(r"^:70E::HOLD\/\/\d*STK.*?\|2(\d*?),{1}(\d*?)\+([A-Z]{3})$")
_MT535_PATCH_LOCK = threading.RLock()


@contextmanager
def _tolerant_mt535_acquisition_price() -> Generator[None, None, None]:
    """Swap python-fints' acquisition-price regex for the tolerant one, then restore it.

    python-fints builds ``MT535_Miniparser()`` inside ``get_holdings`` and the
    raw MT535 text is not reachable from outside, so the only seam is the
    class attribute. The swap is scoped to one get_holdings call, guarded by a
    lock and always undone, so nothing else in the process sees it. ':70E::HOLD'
    clauses that still match neither shape are logged at DEBUG (clause text only:
    it holds no credentials or IBAN).
    """
    try:
        from fints.utils import MT535_Miniparser
    except Exception:  # pragma: no cover - library layout changed; keep the sync working
        yield
        return
    with _MT535_PATCH_LOCK:
        orig_re = MT535_Miniparser.re_acquisitionprice
        orig_collapse = MT535_Miniparser.collapse_multilines

        def _collapse(self: Any, lines: Any) -> Any:
            clauses = orig_collapse(self, lines)
            for clause in clauses:
                if clause.startswith(":70E::HOLD") and not _TOLERANT_ACQUISITION_RE.match(clause):
                    logger.debug("DKB MT535: unmatched acquisition clause %r", clause[:120])
            return clauses

        MT535_Miniparser.re_acquisitionprice = _TOLERANT_ACQUISITION_RE  # type: ignore[assignment]
        MT535_Miniparser.collapse_multilines = _collapse  # type: ignore[method-assign]
        try:
            yield
        finally:
            MT535_Miniparser.re_acquisitionprice = orig_re  # type: ignore[assignment]
            MT535_Miniparser.collapse_multilines = orig_collapse  # type: ignore[method-assign]


class _NullClientContext:
    """Context manager wrapper for python-fints clients that lack __enter__."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def __enter__(self) -> Any:
        return self.client

    def __exit__(self, *_args: Any) -> None:
        return None


class DkbFinTSAdapter:
    """FinTS protocol adapter for DKB.

    Wraps ``python-fints`` (``FinTS3PinTanClient``) to handle:
    - TAN mechanism discovery (DKB-App decoupled push TAN detection)
    - Decoupled push TAN lifecycle (trigger, poll, timeout)
    - SEPA account enumeration, balance reads, transaction reads (90 days), holdings

    All methods raise on failure. ``fetch_snapshot()`` and ``verify_login()``
    return structured dicts suitable for persistence by DkbSyncService.
    """

    def __init__(
        self,
        url: str,
        blz: str,
        username: str,
        pin: str,
        tan_security_function: str | None = None,
        tan_medium: str | None = None,
        product_id: str | None = None,
        system_id: str | None = None,
        push_timeout_seconds: int = 120,
        poll_interval_seconds: int = 5,
        client_factory: Any | None = None,
        sleeper: Any = time.sleep,
    ):
        self.url = url
        self.blz = blz
        self.username = username
        self.pin = pin
        self.tan_security_function = tan_security_function
        self.tan_medium = tan_medium
        self.product_id = product_id
        self.system_id = system_id
        self.push_timeout_seconds = push_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.client_factory = client_factory
        self.sleeper = sleeper
        self.system_id_meta_key: str = "fints_system_id"
        self.debug_fints_logging: bool = False
        self.debug_log: list[str] = []
        # Populated by _configure_tan from the selected mechanism's decoupled-poll
        # parameters; None means "use poll_interval_seconds as fallback".
        self._wait_before_first_poll: int | None = None
        self._wait_before_next_poll: int | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_snapshot(self, state_callback: Any | None = None) -> dict[str, Any]:
        """Open FinTS dialog, drive decoupled push TAN to completion, and
        read accounts / balances / transactions / holdings.

        Returns a dict with keys: ``accounts``, ``transactions``,
        ``positions``, ``errors``, ``system_id`` (optional), ``debug_log``.
        """
        try:
            from fints.client import FinTS3PinTanClient, FinTSOperations, NeedTANResponse
            from fints.models import SEPAAccount
        except Exception as exc:
            raise RuntimeError("python-fints is not installed; install backend dependencies first") from exc

        factory = self.client_factory or FinTS3PinTanClient
        client_kwargs = dict(
            tan_medium=self.tan_medium,
            product_id=self.product_id,
        )
        if self.system_id is not None:
            client_kwargs["system_id"] = self.system_id
        client = factory(
            self.blz, self.username, self.pin, self.url,
            customer_id=None, **client_kwargs,
        )

        debug_log: list[str] = []

        try:
            with _capture_dkb_logs(self.debug_fints_logging, self.pin, self.username) as debug_log:
                logger.info("DKB FinTS: configuring TAN mechanism")
                self._configure_tan(client, state_callback)
                logger.info("DKB FinTS: entering authenticated dialog")
                active_client = client
                context = client if hasattr(client, "__enter__") else _NullClientContext(client)
                try:
                    with context:
                        init_tan = getattr(active_client, "init_tan_response", None)
                        logger.info(
                            "DKB FinTS: dialog entered; init_tan_response=%s",
                            type(init_tan).__name__ if init_tan is not None else None,
                        )
                        while isinstance(getattr(active_client, "init_tan_response", None), NeedTANResponse):
                            active_client.init_tan_response = self._complete_tan(
                                active_client, active_client.init_tan_response, state_callback,
                            )

                        # --- get_information(): read UPD-cached account metadata (no extra FinTS request) ---
                        _info_by_iban: dict[str, dict] = {}
                        _depot_info_accounts: list[dict] = []
                        bank_bic = ""
                        try:
                            info = active_client.get_information()
                            for acc_info in (info.get("accounts") or []):
                                acc_iban = acc_info.get("iban") or ""
                                if acc_iban:
                                    _info_by_iban[acc_iban] = acc_info
                                ops = acc_info.get("supported_operations") or {}
                                if ops.get(FinTSOperations.GET_HOLDINGS, False):
                                    _depot_info_accounts.append(acc_info)
                            if _depot_info_accounts:
                                logger.info(
                                    "DKB FinTS: %d depot account(s) discovered via get_information()",
                                    len(_depot_info_accounts),
                                )
                        except Exception as exc:
                            logger.warning(
                                "DKB FinTS: get_information() failed (depot discovery skipped): %s", exc
                            )

                        # get_sepa_accounts: fatal if it fails (nothing to sync)
                        accounts = self._resolve_response(
                            active_client, active_client.get_sepa_accounts(), state_callback,
                        )
                        logger.info("DKB FinTS: %d SEPA account(s) returned", len(list(accounts or [])))

                        # Exclude from depot loop any account whose IBAN already appears in
                        # the SEPA list — the SEPA loop handles it (including get_holdings).
                        _sepa_ibans = {getattr(a, "iban", "") or "" for a in (accounts or [])} - {""}
                        _depot_info_accounts = [
                            a for a in _depot_info_accounts if (a.get("iban") or "") not in _sepa_ibans
                        ]

                        # Borrow BIC from first SEPA account (all share the same BIC)
                        # needed for constructing SEPAAccount for depot holdings requests
                        for a in (accounts or []):
                            bic_candidate = getattr(a, "bic", "") or ""
                            if len(bic_candidate) >= 6:
                                bank_bic = bic_candidate
                                break

                        errors: list[dict[str, str]] = []
                        balances: list[dict[str, Any]] = []
                        transactions: list[dict[str, Any]] = []
                        positions: list[dict[str, Any]] = []

                        for account in (accounts or []):
                            iban: str = getattr(account, "iban", None) or ""
                            masked = f"...{iban[-4:]}" if len(iban) >= 4 else iban
                            # Look up product_name from UPD via get_information() result
                            _info = _info_by_iban.get(iban)
                            _product_hint = " ".join(filter(None, [
                                str((_info or {}).get("product_name") or ""),
                            ])) if _info else ""
                            account_type = self._account_type(account, product_name_hint=_product_hint)

                            # --- get_balance: skip this account if it fails ---
                            t0 = time.monotonic()
                            parse_causes: list[str] = []
                            try:
                                # python-fints degrades a HISAL balance segment to a bare
                                # FinTS3Segment (no ``balance_booked``) when DKB changes the
                                # response format, then crashes in its own ``_get_balance``.
                                # The FinTSParserWarning only names the outer field; the
                                # cause-chain capture additionally recovers the exact failing
                                # sub-element (e.g. Balance2.date) that robust_mode discards,
                                # so the sync surfaces the precise cause not an opaque
                                # AttributeError.
                                with warnings.catch_warnings(record=True) as parse_warnings, \
                                        _capture_parser_cause_chains(
                                            parse_causes, self.pin, self.username):
                                    warnings.simplefilter("always")
                                    balance = self._resolve_response(
                                        active_client,
                                        active_client.get_balance(account),
                                        state_callback,
                                    )
                                bal_amount = getattr(balance, "amount", balance)
                                if hasattr(bal_amount, "amount"):
                                    bal_amount = bal_amount.amount
                                if isinstance(bal_amount, tuple) and bal_amount:
                                    bal_amount = bal_amount[0]
                                balances.append({
                                    "type": account_type,
                                    "iban": iban,
                                    "balance": Decimal(str(bal_amount or 0)),
                                    "currency": "EUR",
                                })
                                elapsed_ms = (time.monotonic() - t0) * 1000
                                logger.info(
                                    "DKB FinTS: get_balance ok iban=%s type=%s %.0fms",
                                    masked, account_type, elapsed_ms,
                                )
                            except Exception as exc:
                                elapsed_ms = (time.monotonic() - t0) * 1000
                                sanitized = _sanitize_fints_error(str(exc), self.pin, self.username)
                                diag = self._diagnose_balance_failure(
                                    exc, parse_warnings, parse_causes,
                                )
                                if diag:
                                    sanitized = f"{sanitized} [{diag}]"
                                errors.append({"op": "get_balance", "account": masked, "message": sanitized})
                                logger.warning(
                                    "DKB FinTS: get_balance failed iban=%s type=%s %.0fms: %s",
                                    masked, account_type, elapsed_ms, sanitized,
                                )
                                if state_callback:
                                    state_callback(
                                        "warning",
                                        f"Could not read balance for {account_type} {masked}: {sanitized}",
                                    )
                                continue  # skip remaining ops for this account

                            # --- get_transactions: non-fatal per account ---
                            t0 = time.monotonic()
                            try:
                                start = datetime.now(UTC).date() - timedelta(days=90)
                                statements = self._resolve_response(
                                    active_client,
                                    active_client.get_transactions(account, start, datetime.now(UTC).date()),
                                    state_callback,
                                )
                                for statement in (statements or []):
                                    transactions.append(_parse_statement(statement, iban))
                                elapsed_ms = (time.monotonic() - t0) * 1000
                                tx_count = len(list(statements or []))
                                logger.info(
                                    "DKB FinTS: get_transactions ok iban=%s type=%s %d tx %.0fms",
                                    masked, account_type, tx_count, elapsed_ms,
                                )
                            except Exception as exc:
                                elapsed_ms = (time.monotonic() - t0) * 1000
                                sanitized = _sanitize_fints_error(str(exc), self.pin, self.username)
                                errors.append({"op": "get_transactions", "account": masked, "message": sanitized})
                                logger.warning(
                                    "DKB FinTS: get_transactions failed iban=%s type=%s %.0fms: %s",
                                    masked, account_type, elapsed_ms, sanitized,
                                )
                                if state_callback:
                                    state_callback(
                                        "warning",
                                        f"Could not read transactions for {account_type} {masked}: {sanitized}",
                                    )

                            # --- get_holdings: depot only, non-fatal ---
                            holdings_method = getattr(active_client, "get_holdings", None)
                            if holdings_method and account_type == "depot":
                                t0 = time.monotonic()
                                try:
                                    with _tolerant_mt535_acquisition_price():
                                        holdings = self._resolve_response(
                                            active_client, holdings_method(account), state_callback,
                                        )
                                    positions.extend(self._positions_from_holdings(account, holdings or []))
                                    elapsed_ms = (time.monotonic() - t0) * 1000
                                    pos_count = len(list(holdings or []))
                                    logger.info(
                                        "DKB FinTS: get_holdings ok iban=%s %d positions %.0fms",
                                        masked, pos_count, elapsed_ms,
                                    )
                                except Exception as exc:
                                    elapsed_ms = (time.monotonic() - t0) * 1000
                                    sanitized = _sanitize_fints_error(str(exc), self.pin, self.username)
                                    errors.append({"op": "get_holdings", "account": masked, "message": sanitized})
                                    logger.warning(
                                        "DKB FinTS: get_holdings failed iban=%s %.0fms: %s",
                                        masked, elapsed_ms, sanitized,
                                    )
                                    if state_callback:
                                        state_callback(
                                            "warning",
                                            f"Could not read holdings for depot {masked}: {sanitized}",
                                        )

                        # --- Depot accounts (non-SEPA, no IBAN) from get_information() ---
                        for acc_info in _depot_info_accounts:
                            acc_number = str(acc_info.get("account_number") or "")
                            acc_iban = acc_info.get("iban") or acc_number  # fallback: account_number as identifier
                            if not acc_iban:
                                logger.warning("DKB FinTS: skipping depot account with no identifier")
                                continue
                            masked = f"...{acc_iban[-4:]}" if len(acc_iban) >= 4 else acc_iban

                            # Construct SEPAAccount-compatible namedtuple for get_holdings().
                            # Account3.from_sepa_account() reads acc.bic[4:6] for the HBCI country code.
                            if not bank_bic:
                                logger.warning(
                                    "DKB FinTS: no BIC available to construct depot SEPAAccount for %s -- skipping",
                                    masked,
                                )
                                continue
                            try:
                                bi = acc_info.get("bank_identifier")
                                blz = bi.bank_code if bi and hasattr(bi, "bank_code") else self.blz
                                depot_sepa = SEPAAccount(
                                    iban=acc_iban,
                                    bic=bank_bic,
                                    accountnumber=acc_number,
                                    subaccount=str(acc_info.get("subaccount_number") or "0"),
                                    blz=blz,
                                )
                            except Exception as exc:
                                logger.warning(
                                    "DKB FinTS: could not construct SEPAAccount for depot %s: %s", masked, exc,
                                )
                                continue

                            # Depot accounts have no cash balance; add zero-balance account row
                            balances.append({
                                "type": "depot",
                                "iban": acc_iban,
                                "balance": Decimal("0"),
                                "currency": acc_info.get("currency") or "EUR",
                            })

                            # get_holdings -- non-fatal
                            holdings_method = getattr(active_client, "get_holdings", None)
                            if not holdings_method:
                                continue
                            t0 = time.monotonic()
                            try:
                                with _tolerant_mt535_acquisition_price():
                                    holdings = self._resolve_response(
                                        active_client, holdings_method(depot_sepa), state_callback,
                                    )
                                positions.extend(self._positions_from_holdings(depot_sepa, holdings or []))
                                elapsed_ms = (time.monotonic() - t0) * 1000
                                pos_count = len(list(holdings or []))
                                logger.info(
                                    "DKB FinTS: get_holdings ok acct=%s %d position(s) %.0fms",
                                    masked, pos_count, elapsed_ms,
                                )
                            except Exception as exc:
                                elapsed_ms = (time.monotonic() - t0) * 1000
                                sanitized = _sanitize_fints_error(str(exc), self.pin, self.username)
                                errors.append({"op": "get_holdings", "account": masked, "message": sanitized})
                                logger.warning(
                                    "DKB FinTS: get_holdings failed acct=%s %.0fms: %s",
                                    masked, elapsed_ms, sanitized,
                                )
                                if state_callback:
                                    state_callback(
                                        "warning",
                                        f"Could not read holdings for depot {masked}: {sanitized}",
                                    )

                        result: dict[str, Any] = {
                            "accounts": balances,
                            "transactions": transactions,
                            "positions": positions,
                            "errors": errors,
                        }
                        system_id = getattr(active_client, "system_id", None)
                        if system_id and system_id != "0":
                            result["system_id"] = system_id
                        result["debug_log"] = debug_log
                        return result
                except Exception as exc:
                    _is_fints_conn_err = False
                    try:
                        from fints.exceptions import FinTSConnectionError
                        _is_fints_conn_err = isinstance(exc, FinTSConnectionError)
                    except ImportError:
                        pass
                    if _is_fints_conn_err:
                        raise DkbFinTSConnectionRejected() from exc
                    raise
        finally:
            self.debug_log = debug_log[-200:]
            deconstruct = getattr(client, "deconstruct", None)
            if deconstruct:
                deconstruct()

    def verify_login(self, state_callback: Any | None = None) -> dict[str, Any]:
        """Authenticated probe used by the self-test. Opens a real FinTS dialog,
        drives the DKB-App decoupled push to completion, and enumerates SEPA
        accounts -- no transactions, balances, or holdings are read.

        Returns ``{"accounts": <count>, "tan_methods": [...], "decoupled": bool}``.
        Raises ``DkbFinTSManualTanRequired`` if no decoupled method is available
        and ``DkbFinTSTanTimeout`` if the push is not confirmed in time.
        """
        try:
            from fints.client import FinTS3PinTanClient, NeedTANResponse
        except Exception as exc:
            raise RuntimeError("python-fints is not installed; install backend dependencies first") from exc

        factory = self.client_factory or FinTS3PinTanClient
        client_kwargs = dict(
            tan_medium=self.tan_medium,
            product_id=self.product_id,
        )
        if self.system_id is not None:
            client_kwargs["system_id"] = self.system_id
        client = factory(
            self.blz, self.username, self.pin, self.url,
            customer_id=None, **client_kwargs,
        )

        debug_log: list[str] = []

        try:
            with _capture_dkb_logs(self.debug_fints_logging, self.pin, self.username) as debug_log:
                logger.info("DKB FinTS self-test: configuring TAN mechanism")
                self._configure_tan(client, state_callback)
                try:
                    mechanisms = client.get_tan_mechanisms() or {}
                except Exception:
                    mechanisms = {}
                tan_methods = [
                    {"id": str(sf), "name": str(getattr(params, "name", "") or sf)}
                    for sf, params in mechanisms.items()
                ]
                decoupled = self._pick_decoupled_method(mechanisms) is not None
                active_client = client
                context = client if hasattr(client, "__enter__") else _NullClientContext(client)
                logger.info("DKB FinTS self-test: entering authenticated dialog")
                with context:
                    while isinstance(getattr(active_client, "init_tan_response", None), NeedTANResponse):
                        active_client.init_tan_response = self._complete_tan(
                            active_client, active_client.init_tan_response, state_callback,
                        )
                    accounts = self._resolve_response(
                        active_client, active_client.get_sepa_accounts(), state_callback,
                    )
                    count = len(list(accounts or []))
                    system_id = getattr(active_client, "system_id", None)
                logger.info("DKB FinTS self-test: login verified, %d account(s) reachable", count)
                if state_callback:
                    state_callback("confirmed", f"DKB login verified -- {count} account(s) reachable.")
                result: dict[str, Any] = {
                    "accounts": count,
                    "tan_methods": tan_methods,
                    "decoupled": decoupled,
                }
                if system_id and system_id != "0":
                    result["system_id"] = system_id
                result["debug_log"] = debug_log
                return result
        finally:
            self.debug_log = debug_log[-200:]
            deconstruct = getattr(client, "deconstruct", None)
            if deconstruct:
                deconstruct()

    # ------------------------------------------------------------------
    # TAN lifecycle
    # ------------------------------------------------------------------

    def _configure_tan(self, client: Any, state_callback: Any | None) -> None:
        methods_payload: list[dict[str, str]] = []
        mechanisms: dict[str, Any] = {}
        try:
            # python-fints 5.0: get_tan_mechanisms() reads from self.bpd, which
            # is empty on a fresh client. fetch_tan_mechanisms() runs an
            # anonymous BPD dialog to populate it. For DKB this does not raise
            # NeedTANResponse (captured on init_tan_response and consumed by
            # the loop in fetch_snapshot()).
            client.fetch_tan_mechanisms()
            mechanisms = client.get_tan_mechanisms() or {}
        except Exception as exc:
            exc_str = str(exc)
            is_rejected = "Bad status code" in exc_str or "system_id" in exc_str
            if not is_rejected:
                try:
                    from fints.exceptions import FinTSConnectionError
                    if isinstance(exc, FinTSConnectionError):
                        is_rejected = True
                except ImportError:
                    pass
            if is_rejected:
                raise DkbFinTSConnectionRejected() from exc
            logger.warning("DKB fetch_tan_mechanisms failed: %s", exc)
            mechanisms = {}

        for security_function, params in mechanisms.items():
            name = str(getattr(params, "name", "") or security_function)
            tech_id = str(getattr(params, "tech_id", "") or "")
            logger.info(
                "DKB TAN mechanism discovered: sf=%s name=%r tech_id=%r",
                security_function, name, tech_id,
            )
            methods_payload.append({"id": str(security_function), "name": name})

        if state_callback and methods_payload:
            state_callback(
                "pending_tan",
                "DKB TAN methods discovered.",
                available_tan_methods=methods_payload,
            )

        selected = self.tan_security_function or self._pick_decoupled_method(mechanisms)
        if selected:
            logger.info(
                "DKB TAN decoupled method auto-detected: sf=%s mechanisms=%d",
                selected, len(mechanisms),
            )
        else:
            # DKB-specific fallback: try known decoupled push TAN security function 940.
            logger.info(
                "DKB TAN discovery returned no decoupled methods; "
                "trying known DKB security function 940 (decoupled push TAN)"
            )
            try:
                client.set_tan_mechanism("940")
                selected = "940"
                logger.info("DKB TAN fallback to security function 940 succeeded")
            except Exception as fallback_exc:
                logger.warning("DKB fallback to security function 940 failed: %s", fallback_exc)
                raise DkbFinTSManualTanRequired(methods=methods_payload)
        logger.info("DKB TAN mechanism selected: security_function=%s", selected)
        try:
            client.set_tan_mechanism(selected)
        except Exception as exc:
            logger.warning("DKB set_tan_mechanism(%s) failed: %s", selected, exc)

        params = mechanisms.get(selected)
        self._wait_before_first_poll = _coerce_int(getattr(params, "wait_before_first_poll", None))
        self._wait_before_next_poll = _coerce_int(getattr(params, "wait_before_next_poll", None))

    def _pick_decoupled_method(self, mechanisms: dict[str, Any]) -> str | None:
        decoupled_fields = (
            "decoupled_max_poll_number",
            "wait_before_first_poll",
            "wait_before_next_poll",
            "automated_polling_allowed",
        )
        name_keywords = (
            "dkb-app", "dkb app", "app-tan", "apptan", "decoupled push", "pushtan",
        )
        for security_function, params in mechanisms.items():
            if any(getattr(params, f, None) not in (None, "") for f in decoupled_fields):
                return security_function
            tech_id = str(getattr(params, "tech_id", "") or "").lower()
            if "decoupled" in tech_id:
                return security_function
            name = str(getattr(params, "name", "") or "").lower()
            if any(kw in name for kw in name_keywords):
                return security_function
        return None

    def _resolve_response(self, client: Any, response: Any, state_callback: Any | None) -> Any:
        try:
            from fints.client import NeedTANResponse
        except Exception:
            NeedTANResponse = ()  # type: ignore[assignment]
        while isinstance(response, NeedTANResponse):
            response = self._complete_tan(client, response, state_callback)
        return response

    def _diagnose_balance_failure(
        self,
        exc: Exception,
        captured_warnings: Any,
        cause_chains: list[str] | None = None,
    ) -> str:
        """Explain python-fints' opaque generic-segment balance crash.

        A HISAL segment degrades to a bare ``FinTS3Segment`` (no ``balance_booked``)
        in two ways: DKB sends a HISAL version the pinned python-fints has no class
        for (silent -- ``find_subclass`` falls back to the base class), or a known
        version whose field formats changed (``robust_mode`` swallows the
        ``FinTSParserError`` and emits a ``FinTSParserWarning`` naming the field).
        Surface whichever signal is present so the targeted fix is obvious.

        ``cause_chains`` (from :func:`_capture_parser_cause_chains`) is the most
        precise signal: it recovers the exact failing sub-element and the raw
        ``ValueError`` that ``robust_mode`` discards, so prefer it when present.
        """
        if not (isinstance(exc, AttributeError) and "balance_booked" in str(exc)):
            return ""
        balance_chains = [
            c for c in (cause_chains or []) if "HISAL" in c or "Balance" in c
        ]
        if balance_chains:
            return "HISAL parse chain: " + "; ".join(balance_chains)
        parser_msgs = [
            _sanitize_fints_error(str(w.message), self.pin, self.username)
            for w in (captured_warnings or [])
            if "parser error" in str(w.message).lower() or "HISAL" in str(w.message)
        ]
        if parser_msgs:
            return "HISAL parse failed: " + "; ".join(parser_msgs)
        return (
            "HISAL segment fell back to a generic object with no parser warning -- "
            "DKB likely returned an unrecognised HISAL version; enable Settings -> "
            "Integrations -> DKB debug FinTS wire logging to capture the raw segment"
        )

    def _complete_tan(self, client: Any, response: Any, state_callback: Any | None) -> Any:
        message = response.challenge or "Confirm the DKB-App request on your trusted device."
        if state_callback:
            state_callback(
                "waiting_for_push" if getattr(response, "decoupled", False) else "needs_manual_tan",
                message,
                challenge=getattr(response, "challenge", None),
                challenge_html=getattr(response, "challenge_html", None),
                decoupled=bool(getattr(response, "decoupled", False)),
                next_poll_after_seconds=self.poll_interval_seconds
                if getattr(response, "decoupled", False)
                else None,
            )
        logger.info(
            "DKB FinTS: TAN challenge received decoupled=%s challenge=%r",
            bool(getattr(response, "decoupled", False)),
            getattr(response, "challenge", None),
        )
        if not getattr(response, "decoupled", False):
            challenge = getattr(response, "challenge", None)
            detail = (
                "DKB returned a non-decoupled TAN challenge"
                + (f" (challenge: {challenge!r})" if challenge else "")
                + ". A push may have arrived but the bank did not flag it as a DKB-App decoupled"
                " request -- usually the wrong TAN security function is selected."
                " Check Settings -> DKB -> TAN security function, or that DKB-App is your active TAN method."
            )
            raise DkbFinTSManualTanRequired(message=detail)

        # PRIMARY FIX: FinTS decoupled-TAN spec mandates a delay before the
        # first poll (DecoupledMinSecondsBeforePolling, clamped 1-10 s).
        # Polling immediately causes DKB to error instead of returning a clean
        # "still-pending" response (code 3956).
        self.sleeper(self._wait_before_first_poll or self.poll_interval_seconds)

        deadline = time.monotonic() + self.push_timeout_seconds
        current = response
        while time.monotonic() < deadline:
            try:
                current = client.send_tan(current, None)
            except Exception as poll_exc:
                poll_str = str(poll_exc)
                if any(sig in poll_str for sig in _PENDING_TAN_SIGNALS):
                    logger.debug("DKB send_tan poll returned pending signal -- push TAN pending, retrying")
                    if state_callback:
                        state_callback(
                            "waiting_for_push",
                            "Still waiting for DKB-App confirmation.",
                            challenge=getattr(current, "challenge", None),
                            challenge_html=getattr(current, "challenge_html", None),
                            decoupled=True,
                            next_poll_after_seconds=self._wait_before_next_poll or self.poll_interval_seconds,
                        )
                    self.sleeper(self._wait_before_next_poll or self.poll_interval_seconds)
                    continue
                is_rejected = "Bad status code" in poll_str or "system_id" in poll_str
                if not is_rejected:
                    try:
                        from fints.exceptions import FinTSConnectionError
                        if isinstance(poll_exc, FinTSConnectionError):
                            is_rejected = True
                    except ImportError:
                        pass
                if is_rejected:
                    raise DkbFinTSConnectionRejected() from poll_exc
                raise RuntimeError(
                    f"Decoupled TAN poll failed: {type(poll_exc).__name__}: {poll_exc}"
                ) from poll_exc
            try:
                from fints.client import NeedTANResponse
            except Exception:
                NeedTANResponse = ()  # type: ignore[assignment]
            if not isinstance(current, NeedTANResponse):
                return current
            if state_callback:
                state_callback(
                    "waiting_for_push",
                    "Still waiting for DKB-App confirmation.",
                    challenge=getattr(current, "challenge", None),
                    challenge_html=getattr(current, "challenge_html", None),
                    decoupled=True,
                    next_poll_after_seconds=self._wait_before_next_poll or self.poll_interval_seconds,
                )
            self.sleeper(self._wait_before_next_poll or self.poll_interval_seconds)
        raise DkbFinTSTanTimeout(
            "DKB-App confirmation timed out before the bank confirmed the FinTS request."
        )

    # ------------------------------------------------------------------
    # Account / position helpers
    # ------------------------------------------------------------------

    def _account_type(self, account: Any, product_name_hint: str = "") -> str:
        descriptor = " ".join(filter(None, [
            product_name_hint,
            str(getattr(account, "product_name", "") or ""),
            str(getattr(account, "account_type", "") or ""),
            str(getattr(account, "subaccount", "") or ""),
        ])).lower()
        if "depot" in descriptor or "broker" in descriptor:
            return "depot"
        if "visa" in descriptor or "card" in descriptor or "kredit" in descriptor:
            return "visa"
        if ("tagesgeld" in descriptor or "spar" in descriptor or "flexgeld" in descriptor
                or "festgeld" in descriptor or "savings" in descriptor):
            return "tagesgeld"
        return "giro"

    def _positions_from_holdings(self, account: Any, holdings: list[Any]) -> list[dict[str, Any]]:
        result = []
        seen_isins: set[str] = set()
        for holding in holdings:
            isin = getattr(holding, "ISIN", None)  # uppercase — Holding namedtuple field
            if not isin or isin in seen_isins:
                if isin and isin in seen_isins:
                    logger.debug("DKB adapter: dropped duplicate ISIN %s in _positions_from_holdings", isin)
                continue
            seen_isins.add(isin)
            pieces = getattr(holding, "pieces", 0)
            market_value = getattr(holding, "market_value", None)
            total_value = getattr(holding, "total_value", None)
            acq_price = getattr(holding, "acquisitionprice", None)
            name = str(getattr(holding, "name", None) or isin)
            iban_val = getattr(account, "iban", None)
            result.append({
                "iban": iban_val if iban_val else None,
                "isin": str(isin)[:12],
                "name": name,
                "quantity": Decimal(str(pieces or 0)),
                "avg_buy_price": Decimal(str(acq_price)) if acq_price is not None else None,
                "current_price": Decimal(str(market_value)) if market_value is not None else None,
                "current_value": Decimal(str(total_value)) if total_value is not None else None,
            })
        return result
