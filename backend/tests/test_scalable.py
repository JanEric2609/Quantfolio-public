"""Scalable Capital read-only sync: CLI guard, mapping, sync, reconciliation, API.

``sc`` never runs here: a fake runner answers each allowed command with the
documented JSON envelope, so the whole path from argv to ledger rows is real
except the subprocess.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation import broker_status
from app.foundation.core.db import Base
from app.foundation.live_positions import live_positions, pending_reconciliation
from app.foundation.models.entities import (
    ActivityLedgerEntry,
    BrokerPosition,
    BrokerSyncLog,
    ConnectedAccount,
    DkbAccount,
    DkbPosition,
    Holding,
    Portfolio,
    TaxLedgerEvent,
    User,
)
from app.foundation.scalable import cli as sc_cli
from app.foundation.scalable import service as scalable
from app.foundation.scalable import mapping
from app.foundation.scalable.cli import ALLOWED_COMMANDS, MUTATING_WORDS, ScalableCli, ScalableCliConfig, validate_argv
from app.foundation.settings import get_public_settings, upsert_public_settings


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


EUNL = "IE00B4L5Y983"
AAPL = "US0378331005"
TODAY = date.today()
EARLIER = (datetime.now(UTC) - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
NOW = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ok(command: str, data) -> dict:
    return {"ok": True, "command": command, "data": data, "hints": []}


def _broker(command: str, result: dict) -> dict:
    """A broker query as sc emits it: the projection sits in ``result``
    (``broker_result_envelope`` in sc's ``src/broker_shared.rs``)."""
    return _ok(command, {
        "account_id": "acc-1", "portfolio_id": "pf-1",
        "resolution": {"account": "auto_session_person_id", "portfolio": "selected_context"},
        "result": result,
    })


def _overnight(command: str, result: dict) -> dict:
    """The overnight queries wrap their projection the same way (``src/overnight_query_execution.rs``)."""
    return _ok(command, {
        "savings_account_id": "sa-1", "selection": {"account": "auto_single_active"},
        "account": {"display_name": "Tagesgeld", "owner_kind": "self", "is_active": True},
        "result": result,
    })


def _responses() -> dict[tuple[str, ...], dict]:
    return {
        ("whoami",): _ok("whoami", {"logged_in": True}),
        ("capabilities",): _ok("capabilities", {"local_trade_controls": {
            "enabled": True, "isin_controls_active": True, "allowed_isins_configured": True, "allowed_isins": [],
        }}),
        ("broker", "context", "show"): _ok("broker context show", {"context": {"account_id": "acc-1", "portfolio_id": "pf-1"}}),
        ("broker", "overview"): _broker("broker overview", {
            "account_id": "acc-1", "portfolio_id": "pf-1",
            "valuation": {"total": "1650.00", "securities": "1500.00", "crypto": 0},
            "timestamps": {"valuation_timestamp_utc": NOW},
        }),
        ("broker", "cash-breakdown"): _broker("broker cash-breakdown", {"cash_balance": "100.00", "buying_power": 100}),
        ("broker", "holdings"): _broker("broker holdings", {"items": [
            {"isin": EUNL, "name": "iShares Core MSCI World", "security_type": "ETF", "quantity": "10",
             "fifo_price": "80", "valuation": "1000.00", "valuation_currency": "EUR", "quote_mid_price": 100,
             "quote_timestamp_utc": NOW, "quote_is_outdated": False},
            {"isin": AAPL, "name": "Apple‮ Inc", "security_type": "STOCK", "quantity": 5,
             "fifo_price": 90, "valuation": 500, "valuation_currency": "EUR", "quote_mid_price": "100"},
        ]}),
        ("broker", "savings-plans"): _broker("broker savings-plans", {"items": [
            {"isin": EUNL, "name": "iShares Core MSCI World", "amount": "200", "frequency": "MONTHLY", "day_of_month": 2},
        ]}),
        ("overnight",): _overnight("overnight", {"balance": "50.00", "interest_rate": "0.02", "next_payout_date": "2026-10-01"}),
        ("overnight", "transactions"): _overnight("overnight transactions", {"cursor": None, "total": 1, "count": 1, "items": [
            {"id": "on-int-1", "currency": "EUR", "type": "CASH_TRANSACTION", "status": "SETTLED",
             "is_cancellation": False, "last_event_datetime": NOW, "description": "Zinsen",
             "cash_transaction_type": "INTEREST", "amount": "0.42"},
        ]}),
        ("broker", "transactions"): _broker("broker transactions", {"cursor": None, "total": 5, "count": 5, "items": [
            {"summary_type": "BrokerSecurityTransactionSummary", "id": "t-buy", "type": "SECURITY_TRANSACTION",
             "status": "FILLED", "is_cancellation": False, "last_event_datetime": EARLIER, "description": "Buy EUNL",
             "currency": "EUR", "isin": EUNL, "side": "BUY", "quantity": "10", "amount": "800"},
            {"summary_type": "BrokerCashTransactionSummary", "id": "t-dep", "type": "CASH_TRANSACTION",
             "status": "SETTLED", "is_cancellation": False, "last_event_datetime": EARLIER, "description": "Deposit",
             "currency": "EUR", "amount": "2000", "cash_transaction_type": "DEPOSIT"},
            {"summary_type": "BrokerCashTransactionSummary", "id": "t-div", "type": "CASH_TRANSACTION",
             "status": "SETTLED", "is_cancellation": False, "last_event_datetime": EARLIER, "description": "Dividend",
             "currency": "EUR", "related_isin": AAPL, "amount": "5.50", "cash_transaction_type": "DIVIDEND"},
            {"summary_type": "BrokerSecurityTransactionSummary", "id": "t-pending", "type": "SECURITY_TRANSACTION",
             "status": "WAITING_FOR_EXECUTION", "is_cancellation": False, "last_event_datetime": NOW,
             "currency": "EUR", "isin": AAPL, "side": "BUY", "quantity": 1, "amount": 100},
            {"summary_type": "BrokerSecurityTransactionSummary", "id": "t-cancel", "type": "SECURITY_TRANSACTION",
             "status": "FILLED", "is_cancellation": True, "last_event_datetime": EARLIER,
             "currency": "EUR", "isin": AAPL, "side": "BUY", "quantity": 1, "amount": 100},
        ]}),
        ("broker", "transaction", "details"): _broker("broker transaction details", {"id": "t-buy", "security_trade": {
            "status": "FILLED", "side": "BUY", "number_of_shares": {"filled": "10", "total": "10"},
            "average_price": "80.00", "total_amount": "800.99", "fee": "0.99", "taxes": "0",
        }}),
    }


class FakeSc:
    """Stands in for subprocess.run: answers by command path, records every call."""

    def __init__(self, responses: dict | None = None):
        self.responses = responses if responses is not None else _responses()
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.override: dict[tuple[str, ...], tuple[int, dict | str, str]] = {}
        # ``broker transaction details`` answers per --transaction-id when set here.
        self.details: dict[str, dict] = {}

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.kwargs.append(kwargs)
        args = list(argv)
        assert args[-1] == "--json"
        words = tuple(a for a in args[1:-1] if not a.startswith("--"))
        # drop flag values: every flag is followed by one value
        path: list[str] = []
        skip = False
        for a in args[1:-1]:
            if skip:
                skip = False
                continue
            if a.startswith("--"):
                skip = True
                continue
            path.append(a)
        key = tuple(path)
        if key in self.override:
            code, body, stderr = self.override[key]
            out = body if isinstance(body, str) else json.dumps(body)
            return subprocess.CompletedProcess(argv, code, out.encode(), stderr.encode())
        if key == ("broker", "transaction", "details") and "--transaction-id" in args:
            tx_id = args[args.index("--transaction-id") + 1]
            if tx_id in self.details:
                return subprocess.CompletedProcess(argv, 0, json.dumps(self.details[tx_id]).encode(), b"")
        assert key in self.responses, f"unexpected sc call {words}"
        return subprocess.CompletedProcess(argv, 0, json.dumps(self.responses[key]).encode(), b"")

    def ran(self, *path: str) -> int:
        return sum(1 for c in self.calls if tuple(a for a in c[1:len(path) + 1]) == path)


@pytest.fixture
def fake(monkeypatch):
    runner = FakeSc()
    scalable.set_cli_factory(lambda cfg: ScalableCli(
        ScalableCliConfig(wrapper_path="/fake/quantfolio-sc-ro", cli_user="", timeout_seconds=5, max_attempts=2,
                          trusted_wrapper_only=False),
        runner=runner, sleep=lambda s: None,
    ))
    # Post-steps that reach the network in production.
    monkeypatch.setattr("app.foundation.portfolio.isin_resolver.resolve_isin_to_ticker",
                        lambda db, user_id: {"resolved": 0})
    monkeypatch.setattr("app.foundation.etf_classification.classify_and_enrich",
                        lambda db, user_id: {"classified": 0})
    monkeypatch.setattr("app.foundation.tax_cockpit._safe_etf_index", lambda: {})
    yield runner
    scalable.set_cli_factory(None)


def _user(db, name: str = "owner") -> User:
    user = User(username=name, password_hash="x")
    db.add(user)
    db.commit()
    return user


def _enabled(db, **extra) -> None:
    upsert_public_settings(db, {"scalable_enabled": True, **extra})


# --- the argv guard ----------------------------------------------------------


@pytest.mark.parametrize("args", [
    ["whoami"],
    ["capabilities"],
    ["broker", "context", "show"],
    ["broker", "overview", "--portfolio-id", "pf-1"],
    ["broker", "holdings", "--portfolio-id", "pf-1"],
    ["broker", "savings-plans", "--portfolio-id", "pf-1"],
    ["broker", "transactions", "--portfolio-id", "pf-1", "--page-size", "100", "--from-time", "2026-09-01T00:00:00Z"],
    ["broker", "transaction", "details", "--portfolio-id", "pf-1", "--transaction-id", "abc:123"],
    ["overnight"],
])
def test_allowed_reads_pass(args):
    assert validate_argv(args) == args


@pytest.mark.parametrize("args", [
    ["login"],
    ["logout"],
    ["broker", "trade", "buy", "--isin", EUNL],
    ["broker", "context", "select", "--portfolio-id", "pf-1"],
    ["broker", "savings-plans", "create", "--portfolio-id", "pf-1"],
    ["broker", "savings-plans", "--portfolio-id", "pf-1", "--confirm", "true"],
    ["watchlist", "add", "--isin", EUNL],
    ["broker", "overview", "--portfolio-id"],
    ["broker", "overview", "--portfolio-id", "pf-1", "--portfolio-id", "pf-2"],
    ["broker", "overview", "--portfolio-id", "--confirm"],
    ["broker", "overview", "--portfolio-id", "pf-1;rm -rf /"],
    ["broker", "transactions", "--portfolio-id", "pf-1", "--page-size", "1000"],
    ["overnight", "--portfolio-id", "pf-1"],
    ["config", "show"],
])
def test_writes_and_odd_shapes_are_refused(args):
    with pytest.raises(sc_cli.ScalableUsageError):
        validate_argv(args)


def test_allow_list_holds_no_mutating_word():
    for path in ALLOWED_COMMANDS:
        assert not set(path) & MUTATING_WORDS, path


def test_service_only_runs_allow_listed_commands():
    """Every literal command the service passes to ``cli.run`` is on the allow-list."""
    source = (Path(sc_cli.__file__).parent / "service.py").read_text()
    # Literal cli.run(...) calls plus the transactions argv built as a list.
    calls = re.findall(r"cli\.run\(([^)]*)\)", source) + re.findall(r"args = \[([^\]]*)\]", source)
    calls = [c for c in calls if '"' in c]
    assert len(calls) >= 8
    for call in calls:
        words = tuple(re.findall(r'"([a-z-]+)"', call))
        words = tuple(w for w in words if not w.startswith("--"))
        assert any(words[: len(p)] == p for p in ALLOWED_COMMANDS), call
        assert not set(words) & MUTATING_WORDS, call


def test_argv_runs_through_sudo_wrapper_with_scrubbed_env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "do-not-leak")
    runner = FakeSc()
    cli = ScalableCli(ScalableCliConfig(wrapper_path="/usr/local/libexec/quantfolio-sc-ro"), runner=runner)
    assert cli.argv(["whoami"]) == [
        "sudo", "-n", "-H", "-u", "scalable-cli-user", "--", "/usr/local/libexec/quantfolio-sc-ro", "whoami", "--json",
    ]
    calls: list = []

    def record(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, json.dumps(_ok("whoami", {})).encode(), b"")

    ScalableCli(ScalableCliConfig(trusted_wrapper_only=False), runner=record).run("whoami")
    _argv, kwargs = calls[0]
    assert kwargs["shell"] is False
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert set(kwargs["env"]) == {"PATH", "LANG", "LC_ALL"}


def _cli(runner) -> ScalableCli:
    return ScalableCli(ScalableCliConfig(cli_user="", max_attempts=3, trusted_wrapper_only=False),
                       runner=runner, sleep=lambda s: None)


def _err(code: str, exit_code: int):
    def run(argv, **kwargs):
        body = {"ok": False, "command": "x", "error": {"code": code, "message": f"{code} happened"}, "hints": ["h"]}
        return subprocess.CompletedProcess(argv, exit_code, json.dumps(body).encode(), b"")
    return run


def test_exit_codes_map_to_error_types():
    with pytest.raises(sc_cli.ScalableLoginRequired) as exc:
        _cli(_err("refresh_relogin_required", 20)).run("whoami")
    assert exc.value.hints == ["h"]
    with pytest.raises(sc_cli.ScalableUsageError):
        _cli(_err("broker_context_missing", 10)).run("whoami")
    with pytest.raises(sc_cli.ScalableReadOnlyViolation):
        _cli(_err("local_read_only", 1)).run("whoami")


def test_transient_errors_retry_then_raise():
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return _err("rate_limited", 30)(argv)

    with pytest.raises(sc_cli.ScalableTransient):
        _cli(run).run("whoami")
    assert len(calls) == 3


def test_sudo_misconfiguration_and_missing_binary():
    def sudo(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, b"", b"sudo: a password is required")

    with pytest.raises(sc_cli.ScalableNotInstalled) as exc:
        _cli(sudo).run("whoami")
    assert exc.value.code == "sudo_not_configured"

    def missing(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    with pytest.raises(sc_cli.ScalableNotInstalled):
        _cli(missing).run("whoami")


def test_binary_hash_pin(tmp_path):
    binary = tmp_path / "sc"
    binary.write_bytes(b"#!/bin/sh\n")
    cli = ScalableCli(ScalableCliConfig(binary_path=str(binary), binary_sha256="0" * 64, trusted_wrapper_only=False),
                      runner=FakeSc())
    with pytest.raises(sc_cli.ScalableError) as exc:
        cli.run("whoami")
    assert exc.value.code == "binary_hash_mismatch"


@pytest.mark.parametrize("path", ["/bin/sh", "relative/quantfolio-sc-ro", "/tmp/not-there/quantfolio-sc-ro"])
def test_only_a_trusted_wrapper_is_executed(path):
    runner = FakeSc()
    with pytest.raises(sc_cli.ScalableError) as exc:
        ScalableCli(ScalableCliConfig(wrapper_path=path), runner=runner).run("whoami")
    assert exc.value.code in {"wrapper_untrusted", "not_installed"}
    assert runner.calls == []


def test_wrapper_writable_by_others_is_refused(tmp_path):
    wrapper = tmp_path / "quantfolio-sc-ro"
    wrapper.write_text("#!/bin/sh\n")
    wrapper.chmod(0o777)
    runner = FakeSc()
    with pytest.raises(sc_cli.ScalableError) as exc:
        ScalableCli(ScalableCliConfig(wrapper_path=str(wrapper)), runner=runner).run("whoami")
    assert exc.value.code == "wrapper_untrusted"
    assert runner.calls == []


# --- mapping ---------------------------------------------------------------


def test_money_is_decimal_from_strings_numbers_and_objects():
    assert mapping.to_decimal("1.10") == Decimal("1.10")
    assert mapping.to_decimal(0.1) == Decimal("0.1")
    assert mapping.to_decimal({"amount": "2.5", "currency": "EUR"}) == Decimal("2.5")
    assert mapping.to_decimal("NaN") is None
    assert mapping.to_decimal(True) is None


def test_broker_text_is_sanitised():
    assert mapping.clean_text("Apple‮ Inc\x00\n", 50) == "Apple Inc"
    assert mapping.clean_isin(" ie00b4l5y983 ") == EUNL
    assert mapping.clean_isin("not-an-isin") is None


@pytest.mark.parametrize("kind,status,side,cash_type,cancel,expected", [
    ("security", "FILLED", "BUY", None, False, "buy"),
    ("security", "EXECUTED", "SELL", None, False, "sell"),
    ("security", "WAITING_FOR_EXECUTION", "BUY", None, False, None),
    ("security", "FILLED", "BUY", None, True, None),
    ("non_trade", None, None, None, False, "corporate_action"),
    ("cash", "SETTLED", None, "DIVIDEND", False, "dividend"),
    ("cash", "SETTLED", None, "INTEREST_PAYOUT", False, "interest"),
    ("cash", "SETTLED", None, "TAX_PAYMENT", False, "tax"),
    ("cash", "SETTLED", None, "DEPOSIT", False, "cashflow"),
    ("cash", "SETTLED", None, "WITHDRAWAL", False, "cashflow"),
    ("cash", "SETTLED", None, "SOMETHING_NEW", False, "other"),
])
def test_ledger_activity_type(kind, status, side, cash_type, cancel, expected):
    tx = mapping.TransactionRecord(
        id="t", kind=kind, type=None, status=status, is_cancellation=cancel, occurred_at=None,
        description="", currency="EUR", isin=None, side=side, quantity=None, amount=None, cash_type=cash_type,
    )
    assert mapping.ledger_activity_type(tx) == expected


# --- sync ------------------------------------------------------------------


def test_first_sync_writes_positions_accounts_and_ledger(fake):
    db = _memory_db()
    user = _user(db)
    _enabled(db)

    log = scalable.sync(db, user.id)

    # A buy still waiting for execution is skipped, never booked; a known
    # not-executed status is no reason for a warning.
    assert log["state"] == "success", log["message"]
    assert log["message"].startswith("Synced 2 positions")
    positions = {p.isin: p for p in db.query(BrokerPosition).all()}
    assert set(positions) == {EUNL, AAPL}
    assert positions[EUNL].avg_buy_price == Decimal("80")
    assert positions[EUNL].current_value == Decimal("1000.00")
    assert positions[AAPL].name == "Apple Inc"

    accounts = {a.account_type: a for a in db.query(ConnectedAccount).filter_by(source="scalable")}
    assert set(accounts) == {"depot", "cash", "savings"}
    assert accounts["cash"].balance == Decimal("100.00")
    assert accounts["savings"].balance == Decimal("50.00")

    ledger = {e.external_id: e for e in db.query(ActivityLedgerEntry).filter_by(source="scalable")}
    buy = ledger["t-buy"]
    assert buy.activity_type == "buy"
    assert buy.amount == Decimal("-800.99")
    assert buy.price == Decimal("80.00")
    assert buy.fees == Decimal("0.99")
    assert buy.quantity == Decimal("10")
    # Deposits before the first sync are history, not contributions.
    assert ledger["t-dep"].activity_type == "external_pre_sync"
    assert ledger["t-div"].activity_type == "dividend"
    assert "t-pending" not in ledger and "t-cancel" not in ledger
    # The money entering the tracked perimeter is booked once as a contribution.
    opening = ledger["opening:pf-1"]
    assert opening.activity_type == "cashflow"
    assert opening.amount == Decimal("1650.00")
    assert opening.date == TODAY

    # Overnight interest is booked on the overnight account as return.
    interest = ledger["overnight:on-int-1"]
    assert interest.activity_type == "interest"
    assert interest.connected_account_id == accounts["savings"].id

    # Details are fetched only for the new trade and the new dividend.
    assert fake.ran("broker", "transaction", "details") == 2


def test_unknown_status_turns_the_sync_into_a_warning(fake):
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    fake.responses[("broker", "transactions")]["data"]["result"]["items"].append(
        {"summary_type": "BrokerSecurityTransactionSummary", "id": "t-odd", "type": "SECURITY_TRANSACTION",
         "status": "PARTIALLY_SETTLED", "is_cancellation": False, "last_event_datetime": NOW,
         "currency": "EUR", "isin": AAPL, "side": "BUY", "quantity": 1, "amount": 100},
    )
    log = scalable.sync(db, user.id)
    assert log["state"] == "warning"
    assert "PARTIALLY_SETTLED" in log["message"]
    assert "WAITING_FOR_EXECUTION" not in log["message"]


def test_dividend_reaches_tax_ledger_as_estimate(fake):
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    scalable.sync(db, user.id)
    events = db.query(TaxLedgerEvent).filter_by(user_id=user.id, event_type="dividend").all()
    assert len(events) == 1
    event = events[0]
    assert event.event_type == "dividend"
    assert event.gross_eur == Decimal("5.50")
    assert event.source_ref == "scalable:t-div"
    assert event.isin == AAPL


def test_resync_is_idempotent(fake):
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    scalable.sync(db, user.id)
    before = db.query(ActivityLedgerEntry).count()
    taxes = db.query(TaxLedgerEvent).count()
    scalable.sync(db, user.id)
    assert db.query(ActivityLedgerEntry).count() == before
    assert db.query(TaxLedgerEvent).count() == taxes
    assert db.query(BrokerPosition).count() == 2
    # The second run asks only for transactions since the watermark (minus overlap).
    last = [c for c in fake.calls if "transactions" in c][-1]
    assert "--from-time" in last
    assert fake.ran("broker", "transaction", "details") == 2


def test_deposit_after_tracking_is_a_contribution_and_sold_position_disappears(fake):
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    scalable.sync(db, user.id)
    fake.responses[("broker", "transactions")]["data"]["result"]["items"].append(
        {"summary_type": "BrokerCashTransactionSummary", "id": "t-dep2", "status": "SETTLED",
         "is_cancellation": False, "last_event_datetime": NOW, "currency": "EUR", "amount": "300",
         "cash_transaction_type": "DEPOSIT"},
    )
    fake.responses[("broker", "holdings")]["data"]["result"]["items"].pop()  # AAPL sold
    scalable.sync(db, user.id)
    dep = db.query(ActivityLedgerEntry).filter_by(external_id="t-dep2").one()
    assert dep.activity_type == "cashflow"
    assert dep.amount == Decimal("300")
    assert {p.isin for p in db.query(BrokerPosition).all()} == {EUNL}


def test_guard_not_attested_stops_before_reading_the_book(fake):
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    fake.responses[("capabilities",)]["data"]["local_trade_controls"]["allowed_isins"] = [EUNL]
    with pytest.raises(scalable.ScalableGuardNotAttested):
        scalable.sync(db, user.id)
    assert db.query(BrokerPosition).count() == 0
    assert fake.ran("broker", "holdings") == 0
    assert broker_status.status(db, user.id)["state"] == "guard_unattested"


def test_read_only_violation_disables_the_connection(fake):
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    fake.override[("broker", "holdings")] = (
        1, {"ok": False, "command": "broker holdings", "error": {"code": "local_read_only", "message": "no"}}, "",
    )
    with pytest.raises(sc_cli.ScalableReadOnlyViolation):
        scalable.sync(db, user.id)
    assert get_public_settings(db)["scalable_enabled"] is False
    assert db.query(BrokerPosition).count() == 0


def test_expired_login_is_a_warning_with_a_fix(fake):
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    fake.override[("whoami",)] = (
        20, {"ok": False, "command": "whoami", "error": {"code": "no_session", "message": "log in"}}, "",
    )
    with pytest.raises(sc_cli.ScalableLoginRequired):
        scalable.sync(db, user.id)
    log = db.query(BrokerSyncLog).one()
    assert log.state == "warning"
    assert "--local-read-only" in log.message
    assert broker_status.status(db, user.id)["state"] == "login_required"


def test_disabled_and_busy(fake):
    db = _memory_db()
    user = _user(db)
    with pytest.raises(scalable.ScalableDisabled):
        scalable.sync(db, user.id)
    _enabled(db)
    db.add(BrokerSyncLog(user_id=user.id, source="scalable", trigger="manual", state="running",
                         started_at=datetime.now(UTC)))
    db.commit()
    with pytest.raises(scalable.ScalableBusy):
        scalable.sync(db, user.id)


def test_one_sc_login_syncs_into_one_user_only(fake):
    db = _memory_db()
    owner = _user(db, "owner")
    other = _user(db, "other")
    _enabled(db)
    scalable.sync(db, owner.id)
    with pytest.raises(scalable.ScalableOwnedByOtherUser):
        scalable.sync(db, other.id)
    assert db.query(BrokerPosition).filter_by(user_id=other.id).count() == 0


# --- live positions, mirror and reconciliation -------------------------------


def _portfolio(db, user) -> Portfolio:
    from app.foundation.portfolio_service import main_portfolio

    return main_portfolio(db, user.id)


def test_scalable_positions_mirror_into_holdings_and_wealth(fake):
    from app.foundation.portfolio_service import wealth_summary

    db = _memory_db()
    user = _user(db)
    _enabled(db)
    scalable.sync(db, user.id)
    holdings = {h.isin: h for h in db.query(Holding).all()}
    assert set(holdings) == {EUNL, AAPL}
    assert holdings[EUNL].source == "broker_sync"
    assert holdings[EUNL].dkb_available is False

    summary = wealth_summary(db, user.id)
    assert summary["broker_security_value"] == pytest.approx(1500.0)
    assert summary["cash_value"] == pytest.approx(150.0)
    assert summary["total_value"] == pytest.approx(1650.0)
    # The opening balance is a TWR contribution, not this month's income.
    assert summary["cashflow_30d"]["income"] == 0.0


def test_same_isin_at_dkb_and_scalable_is_one_summed_holding(fake):
    from app.foundation.portfolio_service import sync_dkb_positions_to_holdings

    db = _memory_db()
    user = _user(db)
    _enabled(db)
    account = DkbAccount(user_id=user.id, iban="DE00", type="depot", balance=Decimal("0"))
    db.add(account)
    db.flush()
    db.add(DkbPosition(account_id=account.id, isin=EUNL, name="MSCI World", quantity=Decimal("4"),
                       avg_buy_price=Decimal("70"), current_value=Decimal("400")))
    db.commit()
    scalable.sync(db, user.id)
    sync_dkb_positions_to_holdings(db, user.id)
    rows = db.query(Holding).filter_by(isin=EUNL).all()
    assert len(rows) == 1
    assert rows[0].quantity == Decimal("14")
    assert rows[0].source == "dkb_sync"
    assert rows[0].dkb_available is True
    # weighted cost: (4*70 + 10*80) / 14
    assert rows[0].avg_buy_price == pytest.approx(Decimal("1080") / Decimal("14"))


def test_manual_holding_with_same_isin_waits_for_owner_decision(fake):
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    portfolio = _portfolio(db, user)
    manual = Holding(portfolio_id=portfolio.id, isin=EUNL, name="World ETF", asset_type="etf",
                     quantity=Decimal("10"), avg_buy_price=Decimal("75"), source="manual")
    db.add(manual)
    db.commit()
    scalable.sync(db, user.id)

    # Not counted twice: the Scalable EUNL is held back until decided.
    live = {(p.source, p.isin) for p in live_positions(db, user.id)}
    assert ("scalable", EUNL) not in live and ("scalable", AAPL) in live
    assert [p.isin for p in pending_reconciliation(db, user.id)] == [EUNL]

    preview = scalable.reconciliation_preview(db, user.id)
    assert len(preview) == 1 and preview[0]["holding_id"] == manual.id
    assert preview[0]["quantity_matches"] is True
    assert db.get(Holding, manual.id) is not None  # dry run wrote nothing

    result = scalable.apply_reconciliation(db, user.id, {manual.id: "replace"})
    assert result == {"replaced": 1, "kept": 0}
    assert db.get(Holding, manual.id) is None
    assert ("scalable", EUNL) in {(p.source, p.isin) for p in live_positions(db, user.id)}
    mirrored = db.query(Holding).filter_by(isin=EUNL).one()
    assert mirrored.source == "broker_sync"
    assert pending_reconciliation(db, user.id) == []


def test_keep_both_counts_both(fake):
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    portfolio = _portfolio(db, user)
    manual = Holding(portfolio_id=portfolio.id, isin=EUNL, name="World ETF at another broker", asset_type="etf",
                     quantity=Decimal("3"), avg_buy_price=Decimal("75"), source="manual")
    db.add(manual)
    db.commit()
    scalable.sync(db, user.id)
    assert scalable.apply_reconciliation(db, user.id, {manual.id: "keep_both"}) == {"replaced": 0, "kept": 1}
    assert db.get(Holding, manual.id) is not None
    assert ("scalable", EUNL) in {(p.source, p.isin) for p in live_positions(db, user.id)}
    assert pending_reconciliation(db, user.id) == []


def test_monthly_plan_and_tax_cockpit_see_scalable(fake):
    from app.decision.monthly_plan import _holdings
    from app.foundation.tax_cockpit import _depot_positions

    db = _memory_db()
    user = _user(db)
    _enabled(db)
    scalable.sync(db, user.id)
    rows, synced = _holdings(db, user.id)
    scalable_rows = [r for r in rows if r.get("broker") == "scalable"]
    assert {r["isin"] for r in scalable_rows} == {EUNL, AAPL}
    # Synced holdings are not counted again as hand-entered ones.
    assert len(rows) == 2
    assert synced is not None
    assert {p.isin for p in _depot_positions(db, user.id)} == {EUNL, AAPL}


# --- scheduled job -----------------------------------------------------------


def test_scheduled_job_syncs_only_the_owner_when_due(fake):
    from app.foundation.scalable.jobs import scalable_sync_once

    db = _memory_db()
    owner = _user(db, "owner")
    _user(db, "other")
    assert scalable_sync_once(db) == {"status": "skipped", "reason": "disabled"}
    _enabled(db)
    assert scalable_sync_once(db) == {"status": "skipped", "reason": "no_owner"}
    scalable.sync(db, owner.id)
    assert scalable_sync_once(db) == {"status": "skipped", "reason": "not_due"}
    for log in db.query(BrokerSyncLog).all():
        log.started_at = datetime.now(UTC) - timedelta(hours=7)
        log.finished_at = log.started_at
    db.commit()
    result = scalable_sync_once(db)
    assert result["status"] == "ok"
    assert {log.user_id for log in db.query(BrokerSyncLog).all()} == {owner.id}


# --- API -------------------------------------------------------------------


@pytest.fixture
def api(fake):
    from app.foundation.auth import current_user
    from app.foundation.core.db import get_db
    from app.main import app

    db = _memory_db()
    user = _user(db)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    try:
        yield TestClient(app), db, user
    finally:
        app.dependency_overrides.clear()


def test_api_sync_status_positions(api):
    client, db, _user_row = api
    assert client.post("/api/scalable/sync").status_code == 409  # disabled
    _enabled(db)
    resp = client.post("/api/scalable/sync")
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] in {"success", "warning"}
    status = client.get("/api/scalable/status").json()
    assert status["read_only"] is True
    assert status["positions"] == 2
    assert status["portfolio_id"] == "pf-1"
    positions = client.get("/api/scalable/positions").json()
    assert {p["isin"] for p in positions} == {EUNL, AAPL}
    plans = client.get("/api/scalable/savings-plans").json()
    assert plans[0]["isin"] == EUNL
    logs = client.get("/api/scalable/sync/logs").json()
    assert len(logs) == 1


def test_api_login_required_is_424(api, fake):
    client, db, _ = api
    _enabled(db)
    fake.override[("whoami",)] = (
        20, {"ok": False, "command": "whoami", "error": {"code": "no_session", "message": "log in"}}, "",
    )
    resp = client.post("/api/scalable/sync")
    assert resp.status_code == 424
    assert resp.json()["error"]["message"]["code"] == "no_session"


def test_api_reconcile_dry_run_then_confirm(api):
    client, db, user = api
    _enabled(db)
    portfolio = _portfolio(db, user)
    manual = Holding(portfolio_id=portfolio.id, isin=AAPL, name="Apple", asset_type="stock",
                     quantity=Decimal("5"), avg_buy_price=Decimal("90"), source="manual")
    db.add(manual)
    db.commit()
    client.post("/api/scalable/sync")
    preview = client.get("/api/scalable/reconcile").json()
    assert [row["isin"] for row in preview] == [AAPL]
    assert client.post("/api/scalable/reconcile", json={"decisions": {manual.id: "sell"}}).status_code == 422
    done = client.post("/api/scalable/reconcile", json={"decisions": {manual.id: "replace"}}).json()
    assert done == {"replaced": 1, "kept": 0}
    assert client.get("/api/scalable/reconcile").json() == []


def test_probe_names_each_step(fake):
    db = _memory_db()
    _enabled(db)
    result = scalable.probe(db)
    assert result["ok"] is True
    assert [s["name"] for s in result["steps"]] == ["binary", "login", "trade_guard", "portfolio", "overview"]
    assert "1650" not in json.dumps(result)  # no amounts in shared probe output


# --- the root-owned wrapper (second, independent allow-list) -----------------

WRAPPER = Path(__file__).resolve().parents[2] / "infra" / "scalable" / "quantfolio-sc-ro"


@pytest.fixture
def wrapper(tmp_path):
    import getpass
    import shutil

    if shutil.which("bash") is None or shutil.which("flock") is None:
        pytest.skip("bash and flock are needed to run the wrapper")
    fake_sc = tmp_path / "sc"
    fake_sc.write_text('#!/bin/sh\nprintf \'{"ok":true,"command":"fake","data":{"argv":"%s"}}\\n\' "$*"\n')
    fake_sc.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    source = WRAPPER.read_text()
    for old, new in [
        ("readonly SC=/usr/local/bin/sc", f"readonly SC={fake_sc}"),
        ("readonly RUN_AS=scalable-cli-user", f"readonly RUN_AS={getpass.getuser()}"),
        ("readonly RUNTIME=/usr/local/lib/quantfolio-sc-runtime", f"readonly RUNTIME={tmp_path / 'runtime'}"),
        ('home="$(getent passwd "$RUN_AS" | cut -d: -f6)"', f'home="{home}"'),
    ]:
        assert old in source
        source = source.replace(old, new)
    script = tmp_path / "quantfolio-sc-ro"
    script.write_text(source)
    script.chmod(0o755)

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(script), *args], capture_output=True, text=True, timeout=30, check=False)

    run.runtime = tmp_path / "runtime"  # type: ignore[attr-defined]
    run.script = script  # type: ignore[attr-defined]
    run.lock = home / ".config" / "scalable-cli" / ".quantfolio-sc.lock"  # type: ignore[attr-defined]
    return run


_SAMPLE_VALUES = {
    "--portfolio-id": "pf-1",
    "--transaction-id": "abc:123",
    "--cursor": "Y3Vyc29y==",
    "--page-size": "100",
    "--type-filter": "INTEREST",
    "--from-time": "2026-09-01T00:00:00Z",
    "--to-time": "2026-09-30T23:59:59+02:00",
}


def test_wrapper_accepts_exactly_the_python_allow_list(wrapper):
    for path, flags in ALLOWED_COMMANDS.items():
        argv = [*path]
        for flag in sorted(flags):
            argv += [flag, _SAMPLE_VALUES[flag]]
        validate_argv(argv)  # the Python guard agrees
        result = wrapper(*argv, "--json")
        assert result.returncode == 0, (argv, result.stdout)
        assert json.loads(result.stdout)["data"]["argv"] == " ".join([*argv, "--json"])


@pytest.mark.parametrize("args", [
    ["login", "--json"],
    ["broker", "trade", "buy", "--json"],
    ["broker", "context", "select", "--portfolio-id", "pf-1", "--json"],
    ["broker", "savings-plans", "create", "--portfolio-id", "pf-1", "--json"],
    ["broker", "savings-plans", "--portfolio-id", "pf-1", "--confirm", "yes", "--json"],
    ["broker", "overview", "--portfolio-id", "--confirm", "--json"],
    ["broker", "overview", "--portfolio-id", "pf-1;id", "--json"],
    ["broker", "overview", "--portfolio-id", "pf-1", "--portfolio-id", "pf-2", "--json"],
    ["broker", "holdings", "--portfolio-id", "pf-1"],
    ["whoami", "--json", "--json"],
    ["watchlist", "--json"],
])
def test_wrapper_refuses_writes_without_starting_sc(wrapper, args):
    result = wrapper(*args)
    assert result.returncode == 10, result.stdout
    envelope = json.loads(result.stdout)
    assert envelope["ok"] is False
    assert "fake" not in result.stdout


def test_wrapper_starts_sc_through_the_private_glibc_when_installed(wrapper):
    # install.sh puts Debian 13's glibc in RUNTIME on a system whose own is too old for sc.
    runtime = wrapper.runtime
    _fake_loader(runtime)

    result = wrapper("whoami", "--json")
    assert result.returncode == 0, result.stdout
    assert json.loads(result.stdout)["data"]["argv"] == "whoami --json"
    login = wrapper("login", "--local-read-only")
    assert login.returncode == 0, login.stdout
    assert json.loads(login.stdout)["data"]["argv"] == "login --local-read-only"
    assert (runtime / "used").read_text().split() == [str(runtime), str(runtime)]

    refused = wrapper("broker", "trade", "buy", "--json")
    assert refused.returncode == 10
    assert (runtime / "used").read_text().split() == [str(runtime), str(runtime)]


def _fake_loader(runtime):
    runtime.mkdir()
    loader = runtime / "ld.so"
    loader.write_text(
        '#!/bin/sh\n'
        '[ "$1" = --library-path ] || exit 99\n'
        f'printf "%s\\n" "$2" >> "{runtime}/used"\n'
        'shift 2\n'
        'exec "$@"\n'
    )
    loader.chmod(0o755)


def test_wrapper_picks_the_runtime_only_once_it_holds_the_session_lock(wrapper):
    # install.sh replaces RUNTIME while holding this lock; a call that was
    # waiting for it must see the finished runtime, not what was there before.
    import fcntl
    import time

    if not Path("/proc/self/stat").exists():
        pytest.skip("needs /proc to see the wrapper waiting for the lock")
    wrapper.lock.parent.mkdir(parents=True)
    with open(wrapper.lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        proc = subprocess.Popen(
            [str(wrapper.script), "whoami", "--json"], stdout=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not any(  # the wrapper's own `flock -w` child is blocked on the lock
                stat.split()[3] == str(proc.pid) and "(flock)" in stat
                for stat in (_read(path) for path in Path("/proc").glob("[0-9]*/stat"))
            ):
                assert proc.poll() is None and time.monotonic() < deadline, "wrapper never waited for the lock"
                time.sleep(0.02)
            _fake_loader(wrapper.runtime)
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)
        out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 0, out
    assert json.loads(out)["data"]["argv"] == "whoami --json"
    assert (wrapper.runtime / "used").read_text().split() == [str(wrapper.runtime)]


def _read(path):
    try:
        return path.read_text()
    except OSError:  # the process exited in between
        return ""


# Settings that stop `sudo` inside a unit whose User= is not root: NoNewPrivileges
# itself, and every setting that installs a seccomp filter, because systemd then
# turns NoNewPrivileges on by itself (context_has_no_new_privileges, systemd 252+).
_SUDO_BREAKING = {
    "NoNewPrivileges", "RestrictSUIDSGID", "ProtectKernelTunables", "ProtectKernelModules",
    "ProtectKernelLogs", "ProtectClock", "ProtectHostname", "PrivateDevices", "LockPersonality",
    "MemoryDenyWriteExecute", "RestrictRealtime", "RestrictNamespaces", "RestrictAddressFamilies",
    "SystemCallFilter", "SystemCallArchitectures", "SystemCallLog", "DynamicUser",
}


@pytest.mark.parametrize("unit", ["quantfolio-api.service", "quantfolio-worker.service"])
def test_service_units_keep_sudo_working_for_the_scalable_wrapper(unit):
    settings = {}
    for line in (WRAPPER.parents[1] / "systemd" / unit).read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and key.isalpha():
            settings[key] = value.strip()
    assert settings.get("User") not in (None, "", "root")  # the rule only bites a non-root User=
    blocking = {k for k in _SUDO_BREAKING & settings.keys() if settings[k].lower() not in ("", "no", "false", "off", "0")}
    assert not blocking, f"{unit} sets {sorted(blocking)}, which stops sudo to scalable-cli-user"
    assert settings.get("ProtectSystem", "") != "strict" and settings.get("ProtectHome", "no") in ("no", "false")


# --- review hardening (PR 305) -----------------------------------------------


def test_parse_ts_rejects_trailing_garbage():
    assert mapping.parse_ts("2026-01-01not-a-time") is None
    assert mapping.parse_ts("2026-01-01") == datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize("kind,status,cash_type,expected", [
    ("security", None, None, None),  # a trade needs an executed status
    ("security", "", None, None),
    ("cash", "PENDING", "DEPOSIT", None),  # an explicit non-executed cash status waits
    ("cash", None, "DEPOSIT", "cashflow"),  # a booked cash row without a status counts
])
def test_unconfirmed_statuses_are_held_back(kind, status, cash_type, expected):
    tx = mapping.TransactionRecord(
        id="t", kind=kind, type=None, status=status, is_cancellation=False, occurred_at=None,
        description="", currency="EUR", isin=None, side="BUY", quantity=None, amount=None, cash_type=cash_type,
    )
    assert mapping.ledger_activity_type(tx) == expected


def test_bounded_run_caps_stdout_and_stderr(monkeypatch):
    import sys

    monkeypatch.setattr(sc_cli, "MAX_STDOUT_BYTES", 1000)
    monkeypatch.setattr(sc_cli, "MAX_STDERR_BYTES", 500)
    loud = sc_cli.bounded_run([sys.executable, "-c", "import sys; sys.stdout.write('x' * 10_000_000)"], timeout=30)
    assert len(loud.stdout) == 1001  # one byte over the cap: the caller refuses it
    chatty = sc_cli.bounded_run(
        [sys.executable, "-c", "import sys; sys.stderr.write('e' * 200_000); print('{}')"], timeout=30,
    )
    assert chatty.returncode == 0
    assert chatty.stdout.strip() == b"{}"
    assert len(chatty.stderr) <= 501
    with pytest.raises(subprocess.TimeoutExpired):
        sc_cli.bounded_run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)


def test_output_over_the_cap_is_refused(monkeypatch):
    monkeypatch.setattr(sc_cli, "MAX_STDOUT_BYTES", 10)

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, b"x" * 11, b"")

    with pytest.raises(sc_cli.ScalableProtocolError) as exc:
        _cli(run).run("whoami")
    assert exc.value.code == "output_too_large"


def test_failure_envelope_without_error_object_is_a_protocol_error():
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, json.dumps({"ok": False, "error": "boom"}).encode(), b"")

    with pytest.raises(sc_cli.ScalableProtocolError):
        _cli(run).run("whoami")


def test_pinned_hash_is_checked_against_the_binary_the_wrapper_runs():
    cli = ScalableCli(ScalableCliConfig(binary_path="/opt/other/sc", binary_sha256="0" * 64), runner=FakeSc())
    with pytest.raises(sc_cli.ScalableError) as exc:
        cli.check_binary()
    assert exc.value.code == "binary_path_mismatch"
    assert scalable.cli_config({"scalable_binary_path": "/opt/other/sc"}).binary_path == sc_cli.SC_BINARY


def test_only_one_running_sync_per_broker():
    from sqlalchemy.exc import IntegrityError

    db = _memory_db()
    first, second = _user(db, "a"), _user(db, "b")
    db.add(BrokerSyncLog(user_id=first.id, source="scalable", state="running", started_at=datetime.now(UTC)))
    db.commit()
    db.add(BrokerSyncLog(user_id=second.id, source="scalable", state="running", started_at=datetime.now(UTC)))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    # Finished runs are not limited.
    db.add(BrokerSyncLog(user_id=second.id, source="scalable", state="success", started_at=datetime.now(UTC)))
    db.commit()


def test_another_users_running_sync_makes_this_one_busy(fake):
    db = _memory_db()
    first, second = _user(db, "a"), _user(db, "b")
    _enabled(db)
    db.add(BrokerSyncLog(user_id=first.id, source="scalable", state="running", started_at=datetime.now(UTC)))
    db.commit()
    with pytest.raises(scalable.ScalableBusy):
        scalable.sync(db, second.id)


def test_trades_over_the_details_budget_wait_for_the_next_sync(fake, monkeypatch):
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    monkeypatch.setattr(scalable, "MAX_DETAILS_PER_RUN", 0)
    log = scalable.sync(db, user.id)
    # The trade and the dividend both wait: one for price and fees, one for its tax split.
    assert log["counts"]["trades_deferred"] == 2
    assert db.query(ActivityLedgerEntry).filter_by(external_id="t-buy").count() == 0
    depot = broker_status.depot_account(db, user.id)
    watermark = json.loads(depot.raw_json)["transactions_watermark"]
    assert watermark == mapping.parse_ts(EARLIER).isoformat()

    monkeypatch.setattr(scalable, "MAX_DETAILS_PER_RUN", 50)
    scalable.sync(db, user.id)
    buy = db.query(ActivityLedgerEntry).filter_by(external_id="t-buy").one()
    assert buy.price == Decimal("80.00")
    assert buy.fees == Decimal("0.99")


def test_details_briefly_unavailable_defers_the_trade(fake):
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    fake.override[("broker", "transaction", "details")] = (
        30, {"ok": False, "command": "x", "error": {"code": "rate_limited", "message": "slow down"}}, "",
    )
    log = scalable.sync(db, user.id)
    assert log["counts"]["trades_deferred"] == 2
    assert db.query(ActivityLedgerEntry).filter_by(external_id="t-buy").count() == 0


def test_truncated_history_warns_and_keeps_the_watermark(fake, monkeypatch):
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    monkeypatch.setattr(scalable, "FIRST_SYNC_MAX_PAGES", 1)
    fake.responses[("broker", "transactions")]["data"]["result"]["cursor"] = "page-2"
    log = scalable.sync(db, user.id)
    assert log["state"] == "warning"
    assert "older ones were not imported" in log["message"]
    depot = broker_status.depot_account(db, user.id)
    assert "transactions_watermark" not in json.loads(depot.raw_json)


def test_savings_plans_survive_a_failed_read(fake):
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    scalable.sync(db, user.id)
    fake.override[("broker", "savings-plans")] = (
        30, {"ok": False, "command": "x", "error": {"code": "upstream_unavailable", "message": "later"}}, "",
    )
    log = scalable.sync(db, user.id)
    assert log["counts"]["savings_plans"] == 1
    depot = broker_status.depot_account(db, user.id)
    assert json.loads(depot.raw_json)["savings_plans"][0]["isin"] == EUNL


def test_sync_refreshes_the_cached_wealth_summary(fake):
    from app.foundation.portfolio_service import wealth_summary

    db = _memory_db()
    user = _user(db)
    _enabled(db)
    before = wealth_summary(db, user.id)
    assert not before.get("broker_security_value")
    scalable.sync(db, user.id)
    assert wealth_summary(db, user.id)["broker_security_value"] == pytest.approx(1500.0)


def test_numeric_settings_are_clamped_and_validated():
    from app.foundation.settings_catalog import validate_public_settings

    assert broker_status.number_setting({"scalable_sync_hours": 1.5}, "scalable_sync_hours", 6, 1, 48) == 1.5
    assert broker_status.number_setting({"scalable_sync_hours": "daily"}, "scalable_sync_hours", 6, 1, 48) == 6
    assert broker_status.number_setting({"scalable_sync_hours": 1000}, "scalable_sync_hours", 6, 1, 48) == 48
    assert validate_public_settings({"scalable_sync_hours": "daily"}) == {"scalable_sync_hours": "must be a number"}
    assert "scalable_timeout_seconds" in validate_public_settings({"scalable_timeout_seconds": 100_000})
    assert validate_public_settings({"scalable_sync_hours": 1.5}) == {}


def test_only_the_owner_changes_scalable_settings(api):
    client, db, owner = api
    _enabled(db)
    assert client.post("/api/scalable/sync").status_code == 200
    from app.foundation.auth import current_user
    from app.main import app

    other = _user(db, "other")
    app.dependency_overrides[current_user] = lambda: other
    resp = client.put("/api/settings", json={"settings": {"scalable_enabled": False}})
    assert resp.status_code == 403
    assert get_public_settings(db)["scalable_enabled"] is True
    # Re-sending the current value (a form saving every field) is not a change.
    assert client.put("/api/settings", json={"settings": {"scalable_enabled": True}}).status_code == 200
    app.dependency_overrides[current_user] = lambda: owner
    assert client.put("/api/settings", json={"settings": {"scalable_enabled": False}}).status_code == 200


def test_allocation_ignores_other_users_positions(fake):
    from app.foundation.portfolio_service import allocation_by_asset_type

    db = _memory_db()
    me, other = _user(db, "me"), _user(db, "other")
    portfolio = _portfolio(db, me)
    db.add_all([
        Holding(portfolio_id=portfolio.id, isin=EUNL, name="MSCI World", asset_type="etf",
                quantity=Decimal("10"), avg_buy_price=Decimal("100")),
        Holding(portfolio_id=portfolio.id, isin=AAPL, name="Apple", asset_type="stock",
                quantity=Decimal("10"), avg_buy_price=Decimal("100")),
    ])
    other_depot = ConnectedAccount(user_id=other.id, source="scalable", external_id="x", name="d",
                                   institution="Scalable Capital", account_type="depot", currency="EUR")
    db.add(other_depot)
    db.flush()
    db.add(BrokerPosition(user_id=other.id, connected_account_id=other_depot.id, source="scalable", isin=EUNL,
                          name="MSCI World", quantity=Decimal("1"), current_value=Decimal("1000000")))
    db.commit()
    holdings = db.query(Holding).filter_by(portfolio_id=portfolio.id).all()
    allocation = allocation_by_asset_type(holdings)
    assert allocation["etf"] == pytest.approx(allocation["stock"])


def test_harvest_keeps_depots_apart_and_uses_scalables_fee(fake):
    from app.foundation.models.entities import TaxLot
    from app.foundation.tax_cockpit import _harvest_holdings

    db = _memory_db()
    user = _user(db)
    _enabled(db)
    account = DkbAccount(user_id=user.id, iban="DE00", type="depot", balance=Decimal("0"))
    db.add(account)
    db.flush()
    # The DKB depot holds as many EUNL as Scalable, and its lots match that quantity.
    db.add(DkbPosition(account_id=account.id, isin=EUNL, name="MSCI World", quantity=Decimal("10"),
                       avg_buy_price=Decimal("50"), current_price=Decimal("100"), current_value=Decimal("1000")))
    db.add(TaxLot(user_id=user.id, isin=EUNL, account_ref=account.id, acquired_at=date(2020, 1, 2),
                  quantity_initial=Decimal("10"), quantity_remaining=Decimal("10"), cost_basis_eur=Decimal("500")))
    db.commit()
    scalable.sync(db, user.id)
    holdings, _skipped, _warnings = _harvest_holdings(db, user.id)
    at_scalable = [h for h in holdings if h.isin == EUNL and "Scalable" in (h.name or "")]
    assert len(at_scalable) == 1
    # The sync booked Scalable's own lot (the buy, fees included); the DKB lot stays DKB's.
    assert at_scalable[0].average_cost_only is False
    assert [lot.cost_basis_eur for lot in at_scalable[0].lots] == [Decimal("800.99")]
    assert at_scalable[0].order_fee_eur == Decimal("0.99")
    at_dkb = [h for h in holdings if h.isin == EUNL and "Scalable" not in (h.name or "")]
    assert at_dkb[0].average_cost_only is False
    assert at_dkb[0].order_fee_eur is None


# --- login from the Control Center (the one non-read argv) --------------------

from app.foundation.scalable import login as sc_login  # noqa: E402

_PROMPT = (
    "Open this URL:\n{url}\n\nVerify the code \x1b[1mABCD-1234\x1b[0m in your browser.\n\n"
)


def _fake_login(script: str):
    import sys

    def popen(argv, **kwargs):
        assert argv[-2:] == ["login", "--local-read-only"]
        assert "JWT_SECRET" not in (kwargs.get("env") or {})
        return subprocess.Popen([sys.executable, "-c", script], **kwargs)

    return popen


def _wait_done(timeout: float = 10.0) -> dict:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = sc_login.status()
        if state["state"] != "waiting":
            return state
        time.sleep(0.05)
    raise AssertionError("login did not finish")


def _login_cli() -> ScalableCli:
    return ScalableCli(ScalableCliConfig(wrapper_path="/usr/local/libexec/quantfolio-sc-ro", cli_user="u",
                                         trusted_wrapper_only=False))


@pytest.fixture(autouse=False)
def fresh_login():
    sc_login._reset_for_tests()
    yield
    sc_login.cancel()
    sc_login._reset_for_tests()


def test_login_argv_is_the_exact_read_only_login_through_sudo():
    argv = sc_login.login_argv(ScalableCliConfig())
    assert argv == ["sudo", "-n", "-H", "-u", "scalable-cli-user", "--",
                    "/usr/local/libexec/quantfolio-sc-ro", "login", "--local-read-only"]
    # The read path still refuses any login.
    with pytest.raises(sc_cli.ScalableUsageError):
        validate_argv(["login", "--local-read-only"])


def test_login_shows_code_then_reports_read_only_success(fresh_login):
    url = "https://de.scalable.capital/device?user_code=ABCD-1234"
    script = (
        "import sys, time\n"
        f"sys.stdout.write({_PROMPT.format(url=url)!r}); sys.stdout.flush()\n"
        "time.sleep(0.5)\n"
        "print('Logged in via device code.'); print('Local read-only mode is active for this session.')\n"
    )
    started = sc_login.start(_login_cli(), user_id="u1", popen=_fake_login(script), wait_seconds=5)
    assert started["state"] == "waiting"
    assert started["user_code"] == "ABCD-1234"
    assert started["verification_url"] == url
    done = _wait_done()
    assert done["state"] == "succeeded"
    assert done["read_only_confirmed"] is True
    assert done["user_code"] is None  # single-use; not shown after the fact


@pytest.mark.parametrize("url", ["http://de.scalable.capital/device", "https://scalable.capital.evil.example/x",
                                 "https://evil.example/scalable.capital"])
def test_login_never_links_outside_scalable(fresh_login, url):
    script = (
        "import sys, time\n"
        f"sys.stdout.write({_PROMPT.format(url=url)!r}); sys.stdout.flush()\n"
        "time.sleep(0.3)\n"
    )
    started = sc_login.start(_login_cli(), user_id="u1", popen=_fake_login(script), wait_seconds=5)
    assert started["user_code"] == "ABCD-1234"
    assert started["verification_url"] is None


@pytest.mark.parametrize("stderr,code", [
    ('sudo: The "no new privileges" flag is set, which prevents sudo from running as root.', "sandbox_blocks_sudo"),
    ("sudo: a password is required", "sudo_not_configured"),
])
def test_login_names_why_sudo_failed(fresh_login, stderr, code):
    script = f"import sys\nsys.stderr.write({stderr!r} + '\\n')\nsys.exit(1)\n"
    sc_login.start(_login_cli(), user_id="u1", popen=_fake_login(script), wait_seconds=5)
    done = _wait_done()
    assert done["state"] == "failed"
    assert done["error_code"] == code


def test_login_can_be_cancelled(fresh_login):
    script = (
        "import sys, time\n"
        f"sys.stdout.write({_PROMPT.format(url='https://de.scalable.capital/d')!r}); sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    sc_login.start(_login_cli(), user_id="u1", popen=_fake_login(script), wait_seconds=5)
    sc_login.cancel()
    assert _wait_done()["state"] == "cancelled"


def test_sandboxed_sudo_is_named_on_reads():
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, b"", b'sudo: The "no new privileges" flag is set, which prevents sudo from running as root.\n')

    with pytest.raises(sc_cli.ScalableNotInstalled) as exc:
        _cli(run).run("whoami")
    assert exc.value.code == "sandbox_blocks_sudo"


def test_wrapper_accepts_only_the_exact_login(wrapper):
    ok = wrapper("login", "--local-read-only")
    assert ok.returncode == 0, ok.stdout
    assert json.loads(ok.stdout)["data"]["argv"] == "login --local-read-only"
    for args in (["login"], ["login", "--local-read-only", "--json"], ["login", "--local-read-only", "extra"],
                 ["login", "--other"], ["logout", "--json"]):
        refused = wrapper(*args)
        assert refused.returncode == 10, (args, refused.stdout)
        assert "fake" not in refused.stdout


def test_login_api_needs_the_connection_owner_or_an_admin(api, monkeypatch):
    client, db, user = api
    calls = []
    monkeypatch.setattr(sc_login, "start", lambda cli, user_id: calls.append(user_id) or {"state": "waiting"})
    user.role = "user"
    db.commit()
    assert client.post("/api/scalable/login").status_code == 403  # no owner yet: admin only
    user.role = "admin"
    db.commit()
    assert client.post("/api/scalable/login").json()["state"] == "waiting"
    assert calls == [user.id]
    # Once synced into one user, nobody else may replace the login, admin or not.
    _enabled(db)
    assert client.post("/api/scalable/sync").status_code == 200
    from app.foundation.auth import current_user
    from app.main import app

    other = _user(db, "other")
    other.role = "admin"
    db.commit()
    app.dependency_overrides[current_user] = lambda: other
    assert client.post("/api/scalable/login").status_code == 403
    assert client.post("/api/scalable/pin-binary").status_code == 403
    assert client.post("/api/scalable/test").status_code == 403  # the probe runs the owner's sc session
    assert client.post("/api/settings/integrations/test", json={"service": "scalable"}).status_code == 403


def test_pin_binary_stores_the_installed_hash(api, monkeypatch):
    client, db, user = api
    user.role = "admin"
    db.commit()
    import app.interface.api.scalable as scalable_api

    monkeypatch.setattr(scalable_api, "binary_sha256", lambda path: "ab" * 32)
    resp = client.post("/api/scalable/pin-binary")
    assert resp.status_code == 200
    assert resp.json() == {"sha256": "ab" * 32, "pinned": True}
    assert get_public_settings(db)["scalable_binary_sha256"] == "ab" * 32
    monkeypatch.setattr(scalable_api, "binary_sha256", lambda path: None)
    assert client.post("/api/scalable/pin-binary").status_code == 404


# --- sc's result wrapper (the bug that synced zeros) -------------------------


def test_cli_unwraps_the_result_and_keeps_the_resolved_ids():
    runner = FakeSc()
    cli = ScalableCli(ScalableCliConfig(cli_user="", trusted_wrapper_only=False), runner=runner)
    holdings = cli.run("broker", "holdings", "--portfolio-id", "pf-1")
    assert [i["isin"] for i in holdings["items"]] == [EUNL, AAPL]
    assert holdings["portfolio_id"] == "pf-1"
    assert cli.run("overnight")["balance"] == "50.00"
    # Not wrapped: the local context file and the session commands.
    assert cli.run("broker", "context", "show")["context"]["portfolio_id"] == "pf-1"
    assert cli.run("whoami") == {"logged_in": True}


def test_unwrapped_payload_is_a_protocol_error_not_an_empty_depot(fake):
    """The shape the old tests assumed: every field at the top of ``data``.

    Read through the wrapper that made every value None and the sync stored
    zeros while reporting success. A broker query without ``result`` now
    stops the sync before anything is written.
    """
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    fake.responses[("broker", "holdings")] = _ok("broker holdings", {"items": []})
    with pytest.raises(sc_cli.ScalableProtocolError):
        scalable.sync(db, user.id)
    log = db.query(BrokerSyncLog).one()
    assert log.state == "error"
    assert log.error_code == "unexpected_shape"
    assert db.query(BrokerPosition).count() == 0
    assert db.query(ConnectedAccount).count() == 0


@pytest.mark.parametrize("command,result", [
    (("broker", "overview"), {"account_id": "acc-1", "portfolio_id": "pf-1"}),
    (("broker", "cash-breakdown"), {"buying_power": 1}),
    (("broker", "holdings"), {"count": 0}),
    (("broker", "transactions"), {"cursor": None}),
    (("overnight",), {"interest_rate": "0.02"}),
])
def test_a_projection_missing_its_keys_stops_the_sync(fake, command, result):
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    wrap = _overnight if command[0] == "overnight" else _broker
    fake.responses[command] = wrap(" ".join(command), result)
    if command == ("overnight",):
        # The overnight account is optional, but a malformed answer is not "none".
        with pytest.raises(sc_cli.ScalableProtocolError):
            scalable.sync(db, user.id)
        return
    with pytest.raises(sc_cli.ScalableProtocolError):
        scalable.sync(db, user.id)
    assert db.query(BrokerPosition).count() == 0


def test_portfolio_is_resolved_by_sc_when_none_is_set(fake):
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    # A fresh sc install: no saved broker context.
    fake.responses[("broker", "context", "show")] = _ok("broker context show", {"context_file": "x", "context": None})
    log = scalable.sync(db, user.id)
    assert log["state"] == "success", log["message"]
    overview_calls = [c for c in fake.calls if c[1:3] == ["broker", "overview"]]
    assert overview_calls[0][3:] == ["--json"]  # sc picks the account's only portfolio
    holdings_call = next(c for c in fake.calls if c[1:3] == ["broker", "holdings"])
    assert holdings_call[3:5] == ["--portfolio-id", "pf-1"]
    assert broker_status.depot_account(db, user.id).external_id == "pf-1"


def test_empty_holdings_beside_a_valued_depot_is_an_error(fake):
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    fake.responses[("broker", "holdings")]["data"]["result"]["items"] = []
    with pytest.raises(sc_cli.ScalableProtocolError) as exc:
        scalable.sync(db, user.id)
    assert exc.value.code == "holdings_unreadable"
    assert db.query(ConnectedAccount).count() == 0


def test_holdings_far_from_the_overview_value_warn(fake):
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    fake.responses[("broker", "overview")]["data"]["result"]["valuation"]["securities"] = "2000.00"
    log = scalable.sync(db, user.id)
    assert log["state"] == "warning"
    assert "1500.00 EUR" in log["message"] and "2000.00 EUR" in log["message"]


def test_failed_post_step_turns_the_sync_into_a_warning(fake, monkeypatch):
    db = _memory_db()
    user = _user(db)
    _enabled(db)

    def broken(db, user_id):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.foundation.etf_classification.classify_and_enrich", broken)
    log = scalable.sync(db, user.id)
    assert log["state"] == "warning"
    assert "classified" in log["message"]


def test_depot_synced_empty_before_the_fix_is_baselined_again(fake):
    """Syncs that read nothing still set tracking_since; the first real read is the baseline."""
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    stale = (TODAY - timedelta(days=3)).isoformat()
    db.add(ConnectedAccount(
        user_id=user.id, source="scalable", external_id="pf-1", name="Scalable Capital depot",
        institution="Scalable Capital", account_type="depot", currency="EUR", balance=Decimal("0"),
        raw_json=json.dumps({"portfolio_id": "pf-1", "tracking_since": stale}),
    ))
    db.commit()
    # A deposit booked today, before this sync: already inside the opening balance.
    fake.responses[("broker", "transactions")]["data"]["result"]["items"].append(
        {"summary_type": "BrokerCashTransactionSummary", "id": "t-dep-today", "type": "CASH_TRANSACTION",
         "status": "SETTLED", "is_cancellation": False,
         "last_event_datetime": datetime.now(UTC).strftime("%Y-%m-%dT00:00:00Z"),
         "currency": "EUR", "amount": "35", "cash_transaction_type": "DEPOSIT"},
    )
    scalable.sync(db, user.id)
    ledger = {e.external_id: e for e in db.query(ActivityLedgerEntry).filter_by(source="scalable")}
    opening = ledger["opening:pf-1"]
    assert opening.amount == Decimal("1650.00")
    assert opening.date == TODAY
    assert ledger["t-dep-today"].activity_type == "external_pre_sync"
    raw = json.loads(broker_status.depot_account(db, user.id).raw_json)
    assert raw["tracking_since"] == TODAY.isoformat()
    assert raw["baseline_at"]

    # A later sync neither books the opening again nor re-baselines.
    scalable.sync(db, user.id)
    assert db.query(ActivityLedgerEntry).filter_by(external_id="opening:pf-1").count() == 1
    assert json.loads(broker_status.depot_account(db, user.id).raw_json)["baseline_at"] == raw["baseline_at"]


def test_a_depot_already_baselined_keeps_its_opening(fake):
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    scalable.sync(db, user.id)
    depot = broker_status.depot_account(db, user.id)
    raw = json.loads(depot.raw_json)
    raw.pop("baseline_at")  # a depot baselined by an earlier release
    depot.raw_json = json.dumps(raw)
    db.commit()
    scalable.sync(db, user.id)
    assert db.query(ActivityLedgerEntry).filter_by(external_id="opening:pf-1").count() == 1


def test_a_new_portfolio_is_baselined_even_after_another_was_synced(fake):
    """Ledger rows of a different Scalable depot do not make this one's first sync a repeat."""
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    old = ConnectedAccount(user_id=user.id, source="scalable", external_id="pf-old", name="Scalable Capital depot",
                           institution="Scalable Capital", account_type="depot", currency="EUR",
                           balance=Decimal("0"), raw_json="{}")
    db.add(old)
    db.flush()
    db.add(ActivityLedgerEntry(user_id=user.id, connected_account_id=old.id, source="scalable",
                               external_id="t-old", dedupe_hash="old-hash", activity_type="cashflow",
                               date=TODAY - timedelta(days=30), amount=Decimal("100"), description="Deposit"))
    db.commit()
    scalable.sync(db, user.id)
    assert db.query(ActivityLedgerEntry).filter_by(external_id="opening:pf-1").count() == 1


def test_scalable_security_type_sets_the_holding_asset_type(fake):
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    scalable.sync(db, user.id)
    portfolio = _portfolio(db, user)
    types = {h.isin: h.asset_type for h in db.query(Holding).filter_by(portfolio_id=portfolio.id)}
    # A share is never estimated a Vorabpauschale as if it were a fund.
    assert types == {EUNL: "etf", AAPL: "stock"}


def test_a_generic_etf_type_keeps_a_money_market_classification(fake):
    """Scalable reports a money-market fund as "ETF"; the Asset table knows better."""
    from app.foundation.models.entities import Asset

    db = _memory_db()
    user = _user(db)
    _enabled(db)
    db.add(Asset(isin=EUNL, symbol="XEON.DE", name="Money market fund", asset_type="money_market"))
    db.commit()
    scalable.sync(db, user.id)
    scalable.sync(db, user.id)  # the update path too
    portfolio = _portfolio(db, user)
    types = {h.isin: h.asset_type for h in db.query(Holding).filter_by(portfolio_id=portfolio.id)}
    assert types == {EUNL: "money_market", AAPL: "stock"}


# --- tax: lots, sales, withheld tax, interest --------------------------------

SELL_TS = (datetime.now(UTC) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sell(fake, tx_id: str, isin: str, quantity: str, amount: str, *, price: str, fee: str = "0.99",
          taxes: str = "0") -> None:
    fake.responses[("broker", "transactions")]["data"]["result"]["items"].append(
        {"summary_type": "BrokerSecurityTransactionSummary", "id": tx_id, "type": "SECURITY_TRANSACTION",
         "status": "SETTLED", "is_cancellation": False, "last_event_datetime": SELL_TS, "description": "Sell",
         "currency": "EUR", "isin": isin, "side": "SELL", "quantity": quantity, "amount": amount},
    )
    fake.details[tx_id] = _broker("broker transaction details", {"id": tx_id, "security_trade": {
        "status": "SETTLED", "side": "SELL", "number_of_shares": {"filled": quantity, "total": quantity},
        "average_price": price, "total_amount": amount, "fee": fee, "taxes": taxes,
    }})


def test_buys_become_lots_of_the_scalable_depot(fake):
    from app.foundation.models.entities import TaxLot

    db = _memory_db()
    user = _user(db)
    _enabled(db)
    log = scalable.sync(db, user.id)
    lot = db.query(TaxLot).one()
    depot = broker_status.depot_account(db, user.id)
    assert lot.isin == EUNL
    assert lot.account_ref == depot.id  # FIFO per depot
    assert lot.quantity_remaining == Decimal("10")
    assert lot.cost_basis_eur == Decimal("800.99")  # fees are acquisition costs
    assert lot.source == "scalable_sync"
    assert log["counts"]["post_steps"]["tax"]["lots"] == 1
    scalable.sync(db, user.id)
    assert db.query(TaxLot).count() == 1


def test_scalable_sale_consumes_only_scalable_lots_and_books_withheld_tax(fake):
    from app.foundation.models.entities import TaxLot

    db = _memory_db()
    user = _user(db)
    _enabled(db)
    # DKB holds the same ETF with a much older, cheaper lot: FIFO across
    # depots would sell it first and overstate the gain.
    account = DkbAccount(user_id=user.id, iban="DE00", type="depot", balance=Decimal("0"))
    db.add(account)
    db.flush()
    db.add(TaxLot(user_id=user.id, isin=EUNL, account_ref=account.id, acquired_at=date(2015, 1, 2),
                  quantity_initial=Decimal("10"), quantity_remaining=Decimal("10"), cost_basis_eur=Decimal("100"),
                  fund_class="aktien", teilfreistellung_pct=Decimal("0.30")))
    db.commit()
    _sell(fake, "t-sell", EUNL, "4", "399.01", price="100", taxes="5.12")
    scalable.sync(db, user.id)

    sale = db.query(TaxLedgerEvent).filter_by(event_type="sale").one()
    assert sale.source_ref == "scalable:t-sell"
    assert sale.institution == "scalable"
    # 4 x 100 - 0.99 fee, against 4/10 of the 800.99 Scalable lot.
    assert sale.gross_eur == Decimal("399.01")
    assert sale.realised_gain_eur == Decimal("78.61")
    dkb_lot = db.query(TaxLot).filter_by(account_ref=account.id).one()
    assert dkb_lot.quantity_remaining == Decimal("10")
    scalable_lot = db.query(TaxLot).filter_by(source="scalable_sync").one()
    assert scalable_lot.quantity_remaining == Decimal("6")
    withheld = db.query(TaxLedgerEvent).filter_by(event_type="withholding").one()
    assert withheld.withheld_eur == Decimal("5.12")
    assert withheld.source_ref == "scalable:t-sell:tax"

    scalable.sync(db, user.id)
    assert db.query(TaxLedgerEvent).filter_by(event_type="sale").count() == 1
    assert db.query(TaxLedgerEvent).filter_by(event_type="withholding").count() == 1


def test_share_sale_goes_to_the_aktien_loss_pot(fake):
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    fake.responses[("broker", "transactions")]["data"]["result"]["items"].append(
        {"summary_type": "BrokerSecurityTransactionSummary", "id": "t-buy-aapl", "type": "SECURITY_TRANSACTION",
         "status": "SETTLED", "is_cancellation": False, "last_event_datetime": EARLIER, "description": "Buy AAPL",
         "currency": "EUR", "isin": AAPL, "side": "BUY", "quantity": "5", "amount": "-450"},
    )
    fake.details["t-buy-aapl"] = _broker("broker transaction details", {"id": "t-buy-aapl", "security_trade": {
        "status": "SETTLED", "side": "BUY", "number_of_shares": {"filled": "5", "total": "5"},
        "average_price": "90", "total_amount": "450.99", "fee": "0.99", "taxes": "0",
    }})
    _sell(fake, "t-sell-aapl", AAPL, "5", "399.01", price="80")
    scalable.sync(db, user.id)
    sale = db.query(TaxLedgerEvent).filter_by(event_type="sale").one()
    assert sale.bucket == "aktien"
    assert sale.realised_gain_eur < 0
    assert sale.teilfreistellung_pct == Decimal("0")


def test_share_sale_from_a_newer_portfolio_keeps_its_stock_type(fake):
    """Security types of every Scalable depot count, not only the oldest one's."""
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    db.add(ConnectedAccount(user_id=user.id, source="scalable", external_id="pf-old", name="Scalable Capital depot",
                            institution="Scalable Capital", account_type="depot", currency="EUR",
                            balance=Decimal("0"), raw_json="{}",
                            created_at=datetime.now(UTC) - timedelta(days=400)))
    db.commit()
    fake.responses[("broker", "transactions")]["data"]["result"]["items"].append(
        {"summary_type": "BrokerSecurityTransactionSummary", "id": "t-buy-aapl", "type": "SECURITY_TRANSACTION",
         "status": "SETTLED", "is_cancellation": False, "last_event_datetime": EARLIER, "description": "Buy AAPL",
         "currency": "EUR", "isin": AAPL, "side": "BUY", "quantity": "5", "amount": "-450"},
    )
    fake.details["t-buy-aapl"] = _broker("broker transaction details", {"id": "t-buy-aapl", "security_trade": {
        "status": "SETTLED", "side": "BUY", "number_of_shares": {"filled": "5", "total": "5"},
        "average_price": "90", "total_amount": "450.99", "fee": "0.99", "taxes": "0",
    }})
    _sell(fake, "t-sell-aapl", AAPL, "5", "399.01", price="80")
    scalable.sync(db, user.id)
    sale = db.query(TaxLedgerEvent).filter_by(event_type="sale").one()
    assert sale.bucket == "aktien"
    assert sale.teilfreistellung_pct == Decimal("0")


def test_sale_without_lots_is_named_not_guessed(fake):
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    _sell(fake, "t-sell-aapl", AAPL, "1", "99.01", price="100", taxes="3.10")
    log = scalable.sync(db, user.id)
    assert db.query(TaxLedgerEvent).filter_by(event_type="sale").count() == 0
    assert log["state"] == "warning"
    assert "no tax lots at Scalable cover the sale of " + AAPL in log["message"]
    # The tax Scalable withheld was paid even though the gain is not estimated.
    withheld = db.query(TaxLedgerEvent).filter_by(event_type="withholding").one()
    assert withheld.withheld_eur == Decimal("3.10")
    assert withheld.confidence == "estimate"


def test_dividend_uses_the_gross_and_the_tax_scalable_withheld(fake):
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    fake.details["t-div"] = _broker("broker transaction details", {"id": "t-div", "cash": {
        "cash_transaction_type": "DIVIDEND", "amount": "5.50",
        "tax_details": {"gross_amount": "7.47", "tax_amount": "1.97"},
    }})
    scalable.sync(db, user.id)
    dividend = db.query(TaxLedgerEvent).filter_by(event_type="dividend").one()
    assert dividend.gross_eur == Decimal("7.47")
    assert dividend.withheld_eur == Decimal("1.97")
    withheld = db.query(TaxLedgerEvent).filter_by(event_type="withholding").one()
    assert withheld.withheld_eur == Decimal("1.97")
    assert withheld.confidence == "estimate"


def test_overnight_interest_reaches_the_tax_ledger(fake):
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    scalable.sync(db, user.id)
    interest = db.query(TaxLedgerEvent).filter_by(event_type="interest").one()
    assert interest.gross_eur == Decimal("0.42")
    assert interest.institution == "scalable"
    assert interest.source_ref == "scalable:overnight:on-int-1"


def test_overnight_interest_refused_by_an_old_wrapper_is_named(fake):
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    fake.override[("overnight", "transactions")] = (
        10, {"ok": False, "command": "quantfolio-sc-ro",
             "error": {"code": "command_not_allowed", "message": "command not allowed"}, "hints": []}, "",
    )
    log = scalable.sync(db, user.id)
    assert log["counts"]["overnight_interest"] == "unavailable"
    assert db.query(BrokerPosition).count() == 2


def test_year_overview_counts_scalable_events_as_estimates(fake):
    from app.foundation.tax_cockpit import compute_year_overview

    db = _memory_db()
    user = _user(db)
    _enabled(db, tax_estimation_enabled=True)
    fake.details["t-div"] = _broker("broker transaction details", {"id": "t-div", "cash": {
        "cash_transaction_type": "DIVIDEND", "amount": "5.50",
        "tax_details": {"gross_amount": "7.47", "tax_amount": "1.97"},
    }})
    scalable.sync(db, user.id)
    overview = compute_year_overview(db, user.id, TODAY.year)
    assert overview["estimate"] is True
    assert overview["not_tax_advice"] is True
    if EARLIER[:4] == str(TODAY.year):
        assert overview["lines"]["dividends_gross_eur"] == pytest.approx(7.47)
        assert overview["tax"]["kest_already_paid_eur"] == pytest.approx(1.97)


# --- a depot shaped like the payloads Scalable's MCP returns (2026-10-03) -------------

def test_real_account_shape_syncs_every_balance(fake):
    """Three positions, cash, Tagesgeld, savings-plan buys, a pending sell and
    cancelled orders: the values the broken sync stored as zero."""
    vwce, eimi, amc = "IE00BK5BQT80", "IE00BKM4GZ66", "US00165C3025"
    fake.responses.update({
        ("broker", "overview"): _broker("broker overview", {
            "account_id": "acc-1", "portfolio_id": "pf-1",
            "valuation": {"total": 80.56, "securities": 38.46, "crypto": 0},
            "timestamps": {"valuation_timestamp_utc": NOW, "inventory_timestamp_utc": NOW},
            "performance": [],
        }),
        ("broker", "cash-breakdown"): _broker("broker cash-breakdown", {
            "account_id": "acc-1", "portfolio_id": "pf-1", "cash_balance": 42.10, "buying_power": 7.10,
            "pending_buy_orders_amount": 35, "possible_taxes": 0,
        }),
        ("broker", "holdings"): _broker("broker holdings", {"account_id": "acc-1", "portfolio_id": "pf-1",
                                                          "count": 5, "items": [
            {"isin": amc, "name": "AMC Entertainment A", "security_type": "STOCK", "quantity": 1,
             "pending_quantity": 0, "blocked_quantity": 0, "fifo_price": 3.91, "valuation": 2.71,
             "valuation_currency": "EUR", "quote_mid_price": 2.712, "quote_currency": "EUR",
             "quote_timestamp_utc": NOW, "quote_is_outdated": False},
            {"isin": vwce, "name": "Vanguard FTSE All-World (Acc)", "security_type": "ETF", "quantity": 0.152207,
             "pending_quantity": 0, "fifo_price": 164.25, "valuation": 25.63, "valuation_currency": "EUR",
             "quote_mid_price": 168.40, "quote_currency": "EUR", "quote_timestamp_utc": NOW},
            {"isin": eimi, "name": "iShares Core MSCI Emerging Markets IMI (Acc)", "security_type": "ETF",
             "quantity": 0.193424, "pending_quantity": 0, "fifo_price": 51.70, "valuation": 10.12,
             "valuation_currency": "EUR", "quote_mid_price": 52.31, "quote_currency": "EUR",
             "quote_timestamp_utc": NOW},
            # A savings plan without a position yet: no quantity, never a holding.
            {"isin": "NL0010273215", "name": "ASML Holding", "security_type": "STOCK", "quantity": None,
             "valuation": None, "quote_mid_price": 1657.3},
            {"isin": "US67066G1040", "name": "NVIDIA", "security_type": "STOCK", "quantity": None},
        ]}),
        ("overnight",): _overnight("overnight", {"interest_rate": 0.026, "balance": 812.40,
                                                  "current_accrued_amount": 0.07, "next_payout_date": "2026-11-01"}),
        ("overnight", "transactions"): _overnight("overnight transactions",
                                                  {"cursor": None, "total": 0, "count": 0, "items": []}),
        ("broker", "transactions"): _broker("broker transactions", {"cursor": None, "total": 6, "count": 6, "items": [
            {"summary_type": "BrokerSecurityTransactionSummary", "id": "tx-1", "type": "SECURITY_TRANSACTION",
             "status": "PENDING", "is_cancellation": False, "last_event_datetime": NOW, "currency": "EUR",
             "isin": amc, "security_transaction_type": "SINGLE", "quantity": "1", "amount": "0", "side": "SELL"},
            {"summary_type": "BrokerSecurityTransactionSummary", "id": "tx-2", "type": "SECURITY_TRANSACTION",
             "status": "SETTLED", "is_cancellation": False, "last_event_datetime": EARLIER, "currency": "EUR",
             "isin": amc, "security_transaction_type": "SINGLE", "quantity": "1", "amount": "-3.91", "side": "BUY"},
            {"summary_type": "BrokerCashTransactionSummary", "id": "tx-3", "type": "CASH_TRANSACTION",
             "status": "SETTLED", "is_cancellation": False, "last_event_datetime": EARLIER, "currency": "EUR",
             "description": "Scalable Capital Broker savings plan", "cash_transaction_type": "DEPOSIT",
             "amount": "35"},
            {"summary_type": "BrokerSecurityTransactionSummary", "id": "tx-4", "type": "SECURITY_TRANSACTION",
             "status": "SETTLED", "is_cancellation": False, "last_event_datetime": EARLIER, "currency": "EUR",
             "isin": eimi, "security_transaction_type": "SAVINGS_PLAN", "quantity": "0.193424", "amount": "-10",
             "side": "BUY"},
            {"summary_type": "BrokerSecurityTransactionSummary", "id": "tx-5", "type": "SECURITY_TRANSACTION",
             "status": "SETTLED", "is_cancellation": False, "last_event_datetime": EARLIER, "currency": "EUR",
             "isin": vwce, "security_transaction_type": "SAVINGS_PLAN", "quantity": "0.152207",
             "amount": "-24.9999", "side": "BUY"},
            {"summary_type": "BrokerSecurityTransactionSummary", "id": "tx-6", "type": "SECURITY_TRANSACTION",
             "status": "CANCELLED", "is_cancellation": False, "last_event_datetime": EARLIER, "currency": "EUR",
             "isin": eimi, "security_transaction_type": "SINGLE", "quantity": "1", "amount": "0", "side": "BUY"},
        ]}),
    })
    fake.responses[("broker", "transaction", "details")] = _broker(
        "broker transaction details", {"id": "x", "security_trade": None, "cash": None})
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    log = scalable.sync(db, user.id)
    assert log["state"] == "success", log["message"]
    assert log["counts"]["positions"] == 3

    accounts = {a.account_type: a for a in db.query(ConnectedAccount).filter_by(source="scalable")}
    assert accounts["depot"].balance == Decimal("38.46")
    assert accounts["cash"].balance == Decimal("42.10")
    assert accounts["savings"].balance == Decimal("812.40")
    positions = {p.isin: p for p in db.query(BrokerPosition)}
    assert positions[vwce].quantity == Decimal("0.152207")
    assert positions[amc].security_type == "STOCK"

    ledger = {e.external_id: e for e in db.query(ActivityLedgerEntry).filter_by(source="scalable")}
    assert ledger["opening:pf-1"].amount == Decimal("892.96")  # 38.46 + 42.10 + 812.40
    assert ledger["tx-3"].activity_type == "external_pre_sync"
    assert ledger["tx-5"].activity_type == "buy"
    assert "tx-1" not in ledger and "tx-6" not in ledger
    # Fractional savings-plan buys become lots.
    from app.foundation.models.entities import TaxLot

    lots = {lot.isin: lot for lot in db.query(TaxLot)}
    assert lots[vwce].quantity_remaining == Decimal("0.152207")
    assert lots[vwce].cost_basis_eur == Decimal("24.9999")
    portfolio = _portfolio(db, user)
    types = {h.isin: h.asset_type for h in db.query(Holding).filter_by(portfolio_id=portfolio.id)}
    assert types == {amc: "stock", vwce: "etf", eimi: "etf"}


def test_overnight_interest_history_is_read_once_the_wrapper_allows_it(fake):
    db = _memory_db()
    user = _user(db)
    _enabled(db)
    fake.override[("overnight", "transactions")] = (
        10, {"ok": False, "command": "quantfolio-sc-ro",
             "error": {"code": "command_not_allowed", "message": "command not allowed"}, "hints": []}, "",
    )
    scalable.sync(db, user.id)
    del fake.override[("overnight", "transactions")]
    fake.calls.clear()
    scalable.sync(db, user.id)
    overnight_call = next(c for c in fake.calls if c[1:3] == ["overnight", "transactions"])
    assert "--from-time" not in overnight_call  # the whole history, not since the watermark
    assert db.query(TaxLedgerEvent).filter_by(event_type="interest").count() == 1
    fake.calls.clear()
    scalable.sync(db, user.id)
    overnight_call = next(c for c in fake.calls if c[1:3] == ["overnight", "transactions"])
    assert "--from-time" in overnight_call


def test_lot_cost_adds_a_fee_the_booked_amount_leaves_out(fake):
    from app.foundation.models.entities import TaxLot

    db = _memory_db()
    user = _user(db)
    _enabled(db)
    # total_amount 800.00 = 10 x 80 without the 0.99 fee.
    fake.responses[("broker", "transaction", "details")]["data"]["result"]["security_trade"]["total_amount"] = "800.00"
    scalable.sync(db, user.id)
    assert db.query(TaxLot).one().cost_basis_eur == Decimal("800.99")
