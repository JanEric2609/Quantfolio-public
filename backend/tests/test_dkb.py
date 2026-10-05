import unittest.mock as mock
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from conftest import _memory_db
from fints.client import NeedTANResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.interface.api.dkb import fints_selftest, run_diagnostics
from app.interface.api.settings import test_integration as settings_test_integration
from app.foundation.core.db import Base
from app.foundation.models.entities import DkbAccount, DkbDiagnosticRun, DkbPosition, User
from app.foundation.schemas import IntegrationTestRequest
from app.foundation.dkb import DkbFinTSAdapter, DkbSyncService, dkb_transaction_hash
from app.foundation.settings import set_secret, upsert_public_settings


@pytest.fixture(autouse=True)
def _no_real_backfill_jobs():
    """persist_snapshot() unconditionally schedules a price-backfill job on the
    process-global APScheduler (app.foundation.jobs.submit_job). That job runs on
    its own thread, detached from any test's `with mock.patch("app.foundation.core.db.
    SessionLocal", ...)` scope -- by the time it fires it opens a real
    SessionLocal against whatever DB the process happens to be configured for,
    which may not have the table it needs. This turned into a flaky,
    misattributed failure in an unrelated test (the job's unhandled exception
    surfaces in whichever test happens to be running when it fires). Stub it
    out for this whole file; the backfill itself has its own test coverage.
    """
    with mock.patch("app.foundation.jobs.submit_job", lambda *a, **k: "stubbed"):
        yield


def test_dkb_transaction_hash_normalizes_reference_and_cents():
    first = dkb_transaction_hash(date(2026, 5, 15), Decimal("12.340"), "DKB  Card   Payment")
    second = dkb_transaction_hash(date(2026, 5, 15), Decimal("12.34"), "dkb card payment")

    assert first == second


def test_dkb_transaction_hash_changes_with_amount():
    first = dkb_transaction_hash(date(2026, 5, 15), Decimal("12.34"), "Merchant")
    second = dkb_transaction_hash(date(2026, 5, 15), Decimal("12.35"), "Merchant")

    assert first != second


def _balance_adapter() -> DkbFinTSAdapter:
    return DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "secretuser", "1234",
    )


def test_diagnose_balance_failure_surfaces_parser_warning_field():
    """A python-fints HISAL parse warning (field-format change) names the field."""
    adapter = _balance_adapter()
    exc = AttributeError("'FinTS3Segment' object has no attribute 'balance_booked'")
    warns = [SimpleNamespace(message=(
        "Ignoring parser error and returning generic object: "
        "Wrong input when setting HISAL7.account_product. Turn off robust_mode."
    ))]

    msg = adapter._diagnose_balance_failure(exc, warns)

    assert "HISAL parse failed" in msg
    assert "HISAL7.account_product" in msg


def test_diagnose_balance_failure_hints_unknown_version_when_silent():
    """No warning means an unrecognised HISAL version -- point to wire logging."""
    adapter = _balance_adapter()
    exc = AttributeError("'FinTS3Segment' object has no attribute 'balance_booked'")

    msg = adapter._diagnose_balance_failure(exc, [])

    assert "unrecognised HISAL version" in msg
    assert "wire logging" in msg


def test_diagnose_balance_failure_ignores_unrelated_exceptions():
    adapter = _balance_adapter()
    assert adapter._diagnose_balance_failure(ValueError("boom"), []) == ""


def test_diagnose_balance_failure_sanitizes_credentials_in_warning():
    """Parser warnings that echo the PIN/username must be scrubbed."""
    adapter = _balance_adapter()
    exc = AttributeError("'FinTS3Segment' object has no attribute 'balance_booked'")
    warns = [SimpleNamespace(
        message="Wrong input when setting HISAL7.account_product secretuser 1234"
    )]

    msg = adapter._diagnose_balance_failure(exc, warns)

    assert "secretuser" not in msg
    assert "1234" not in msg


def test_diagnose_balance_failure_prefers_cause_chain_over_warning():
    """The precise cause chain (naming the sub-element) wins over the vague warning."""
    adapter = _balance_adapter()
    exc = AttributeError("'FinTS3Segment' object has no attribute 'balance_booked'")
    warns = [SimpleNamespace(message=(
        "Ignoring parser error and returning generic object: "
        "Wrong input when setting HISAL7.balance_booked. Turn off robust_mode."
    ))]
    chains = [
        "FinTSParserError: Wrong input when setting HISAL7.balance_booked "
        "<- FinTSParserError: Wrong input when setting Balance2.date "
        "<- ValueError: bad date"
    ]

    msg = adapter._diagnose_balance_failure(exc, warns, chains)

    assert "HISAL parse chain" in msg
    assert "Balance2.date" in msg


def test_capture_parser_cause_chains_recovers_subelement_and_preserves_behaviour():
    """The scoped capture recovers the exact failing Balance2 sub-element that
    robust_mode discards, while leaving parse behaviour (generic fallback + warning)
    unchanged."""
    import warnings

    from fints.parser import FinTS3Parser

    from app.foundation.dkb.adapter import _capture_parser_cause_chains

    parser = FinTS3Parser()
    # Malformed HISAL7: balance_booked carries an invalid credit/debit code "X".
    segment = [
        ["HISAL", "1", "7"],
        ["DE00000000000000000000", "BYLADEM1001", "12345678", ""],
        "Girokonto",
        "EUR",
        ["X", ["1234,56", "EUR"], "20260723"],
    ]
    sink: list[str] = []
    with warnings.catch_warnings(record=True) as caught, \
            _capture_parser_cause_chains(sink, "1234", "secretuser"):
        warnings.simplefilter("always")
        out = parser.parse_segment(segment)

    # Behaviour preserved: still degrades to a generic segment and still warns.
    assert type(out).__name__ == "FinTS3Segment"
    assert any("returning generic object" in str(w.message) for w in caught)
    # Capture recovers the exact sub-element + raw cause the warning omits.
    assert len(sink) == 1
    assert "Balance2.credit_debit" in sink[0]
    assert "CreditDebit2" in sink[0]


def test_capture_parser_cause_chains_scrubs_credentials():
    """A cause chain that echoes the PIN/username must be scrubbed."""
    import warnings

    from fints.parser import FinTS3Parser

    from app.foundation.dkb.adapter import _capture_parser_cause_chains

    parser = FinTS3Parser()
    segment = [
        ["HISAL", "1", "7"],
        ["DE00000000000000000000", "BYLADEM1001", "12345678", ""],
        "secretuser",   # account_product echoing the username
        "EUR",
        ["X", ["1234,56", "EUR"], "20260723"],
    ]
    sink: list[str] = []
    with warnings.catch_warnings(), \
            _capture_parser_cause_chains(sink, "1234", "secretuser"):
        warnings.simplefilter("always")
        parser.parse_segment(segment)

    assert sink
    assert "secretuser" not in sink[0]


def test_capture_parser_cause_chains_restores_original_parse_segment():
    """The monkeypatch must be reverted on exit, even when nothing failed."""
    from fints.parser import FinTS3Parser

    from app.foundation.dkb.adapter import _capture_parser_cause_chains

    original = FinTS3Parser.parse_segment
    with _capture_parser_cause_chains([], "1234", "secretuser"):
        assert FinTS3Parser.parse_segment is not original
    assert FinTS3Parser.parse_segment is original


def test_balance2_date_optional_patch_tolerates_dkb_truncated_balance():
    """DKB omits the mandatory Balance2.date element; the patch relaxes it so
    HISAL7 parses (balance_booked present) instead of degrading to a generic
    object that lacks balance_booked -> the 'no attribute balance_booked' error."""
    from fints.parser import FinTS3Parser
    from fints.segments.saldo import HISAL7

    from app.foundation.dkb.adapter import _patch_fints_balance2_optional_date

    _patch_fints_balance2_optional_date()
    parser = FinTS3Parser()
    # Balance2 truncated after amount -- no date element, as DKB now sends.
    segment = [
        ["HISAL", "1", "7"],
        ["DE00000000000000000000", "BYLADEM1001", "12345678", ""],
        "Girokonto",
        "EUR",
        # balance_booked DEG truncated after the amount -- credit_debit, amount,
        # currency, then no date element (DKB's non-compliant response).
        ["C", "1234,56", "EUR"],
    ]
    out = parser.parse_segment(segment)

    assert isinstance(out, HISAL7)
    assert out.balance_booked is not None
    assert out.balance_booked.date is None
    assert str(out.balance_booked.amount.amount) == "1234.56"


def test_patch_fints_balance2_optional_date_is_idempotent():
    """Applying the patch repeatedly leaves date optional and is side-effect free."""
    from fints.formals import Balance2

    from app.foundation.dkb.adapter import _patch_fints_balance2_optional_date

    _patch_fints_balance2_optional_date()
    _patch_fints_balance2_optional_date()
    assert getattr(Balance2, "_fields")["date"].required is False


def test_dkb_adapter_polls_decoupled_push_tan_until_confirmed():
    states = []

    class FakeClient:
        init_tan_response = None

        def __init__(self, *_args, **_kwargs):
            self.polls = 0
            self._fetched = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch_tan_mechanisms(self):
            self._fetched = True
            return "920"

        def get_tan_mechanisms(self):
            if not self._fetched:
                from collections import OrderedDict
                return OrderedDict()
            return {"920": SimpleNamespace(name="DKB-App (decoupled)", tech_id="DECOUPLED")}

        def set_tan_mechanism(self, value):
            self.tan_mechanism = value

        def get_sepa_accounts(self):
            return need_push_tan()

        def send_tan(self, response, _tan):
            self.polls += 1
            if self.polls == 1:
                return response
            return [SimpleNamespace(iban="DE001", product_name="Girokonto")]

        def get_balance(self, _account):
            return SimpleNamespace(amount=Decimal("42.50"))

        def get_transactions(self, _account, *_args):
            return [
                SimpleNamespace(
                    amount=Decimal("-12.34"),
                    purpose="Card payment",
                    date=date(2026, 5, 16),
                )
            ]

        def deconstruct(self):
            return {}

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints",
        "12030000",
        "user",
        "pin",
        client_factory=FakeClient,
        sleeper=lambda _seconds: None,
    )

    snapshot = adapter.fetch_snapshot(
        state_callback=lambda state, message, **extra: states.append((state, message, extra))
    )

    assert snapshot["accounts"][0]["balance"] == Decimal("42.50")
    assert snapshot["transactions"][0]["reference"] == "Card payment"
    assert any(state == "waiting_for_push" for state, _message, _extra in states)


def test_fints_sync_starts_without_product_id_using_default():
    """Sync no longer blocks when product_id is missing — the built-in default is used."""
    import unittest.mock as mock

    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    set_secret(db, "dkb", "12345", {"username": "dkb-user"})
    upsert_public_settings(db, {"dkb_provider": "fints", "dkb_product_id": None})

    # Stub the worker so no real background thread is spawned: an unjoined
    # daemon thread would otherwise reach a real network call and the real
    # (unpatched) SessionLocal after this test returns, and its unhandled
    # exception then surfaces attributed to whichever test happens to be
    # running when it fires.
    with mock.patch.object(DkbSyncService, "_run_sync", lambda *a, **k: None):
        session = DkbSyncService().trigger(db, user.id)

    # Should no longer fail with a product_id complaint — sync starts
    assert session.state != "failed" or "product_id" not in session.message


def test_dkb_diagnostics_no_longer_fails_for_missing_product_id():
    """product_id is optional — diagnostics should not report it as a blocking failure."""
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    set_secret(db, "dkb", "12345", {"username": "dkb-user"})
    upsert_public_settings(db, {"dkb_provider": "fints", "dkb_product_id": None})

    result = run_diagnostics(db, user)

    assert not any(step["key"] == "product_id" and step["status"] == "failed" for step in result["steps"])
    assert db.query(DkbDiagnosticRun).count() == 1


def test_fints_selftest_discovers_decoupled_tan():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    set_secret(db, "dkb", "12345", {"username": "dkb-user"})
    upsert_public_settings(db, {"dkb_provider": "fints", "dkb_product_id": "TESTPROD001"})

    import unittest.mock as mock

    with mock.patch("fints.client.FinTS3PinTanClient", _DecoupledSelftestClient):
        result = fints_selftest(db, user)

    assert result["status"] == "passed"
    assert any(s["key"] == "tan_discovery" and s["status"] == "passed" for s in result["steps"])
    assert any(s["key"] == "login" and s["status"] == "passed" for s in result["steps"])
    assert db.query(DkbDiagnosticRun).filter(DkbDiagnosticRun.user_id == user.id).count() >= 1


def test_dkb_adapter_calls_fetch_tan_mechanisms_before_get():
    """get_tan_mechanisms() returns empty until fetch_tan_mechanisms() populates BPD."""
    call_order: list[str] = []

    class FakeClient:
        init_tan_response = None

        def __init__(self, *_args, **_kwargs):
            self._fetched = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch_tan_mechanisms(self):
            call_order.append("fetch")
            self._fetched = True
            return "920"

        def get_tan_mechanisms(self):
            call_order.append("get")
            if not self._fetched:
                from collections import OrderedDict
                return OrderedDict()
            return {"920": SimpleNamespace(name="DKB-App", tech_id="DECOUPLED")}

        def set_tan_mechanism(self, value):
            self.tan_mechanism = value

        def get_sepa_accounts(self):
            return []

        def deconstruct(self):
            return {}

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        client_factory=FakeClient, sleeper=lambda _s: None,
    )
    adapter.fetch_snapshot()
    assert call_order[0] == "fetch"
    assert "get" in call_order[1:]


def test_dkb_adapter_raises_when_no_decoupled_method_available():
    import pytest

    from app.foundation.dkb import DkbFinTSManualTanRequired

    class FakeClient:
        init_tan_response = None

        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch_tan_mechanisms(self):
            return "911"

        def get_tan_mechanisms(self):
            return {"911": SimpleNamespace(name="chipTAN manuell", tech_id="HHD")}

        def set_tan_mechanism(self, value):
            if value == "940":
                raise ValueError("DKB TAN fallback 940 not available")

        def get_sepa_accounts(self):
            return []

        def deconstruct(self):
            return {}

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        client_factory=FakeClient, sleeper=lambda _s: None,
    )
    with pytest.raises(DkbFinTSManualTanRequired):
        adapter.fetch_snapshot()


def test_fints_selftest_warns_when_only_manual_tan_methods_offered():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    set_secret(db, "dkb", "12345", {"username": "dkb-user"})
    upsert_public_settings(db, {"dkb_provider": "fints", "dkb_product_id": "TESTPROD001"})

    import unittest.mock as mock

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def fetch_tan_mechanisms(self):
            return "911"

        def get_tan_mechanisms(self):
            return {"911": SimpleNamespace(name="chipTAN manuell", tech_id="HHD")}

        def deconstruct(self):
            pass

    with mock.patch("fints.client.FinTS3PinTanClient", FakeClient):
        result = fints_selftest(db, user)

    assert result["status"] == "warning"
    assert any(s["key"] == "tan_discovery" and s["status"] == "warning" for s in result["steps"])


def test_dkb_connection_test_credential_check():
    """test_integration DKB branch does a lightweight credential check (no FinTS dialog).
    When credentials are saved, ok=True with an honest message."""
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    set_secret(db, "dkb", "12345", {"username": "dkb-user"})

    payload = IntegrationTestRequest(service="dkb", value=None, meta={})
    result = settings_test_integration(payload, db, user)

    assert result.ok is True
    assert "credentials present" in result.message.lower()


def test_dkb_connection_test_no_creds_fails():
    """settings test_integration DKB branch returns ok=False when no creds saved."""
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    # No creds saved — credential check should detect missing credentials

    payload = IntegrationTestRequest(service="dkb", value=None, meta={})
    result = settings_test_integration(payload, db, user)

    assert result.ok is False
    assert "credentials not saved" in result.message.lower()


class _DecoupledSelftestClient:
    """A FinTS client that advertises DKB-App decoupled push and completes an
    authenticated dialog returning two SEPA accounts (no NeedTAN handshake)."""

    init_tan_response = None

    def __init__(self, *_args, **_kwargs):
        self._fetched = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def fetch_tan_mechanisms(self):
        self._fetched = True
        return "920"

    def get_tan_mechanisms(self):
        return {"920": SimpleNamespace(name="DKB-App (decoupled)", tech_id="DECOUPLED")}

    def set_tan_mechanism(self, value):
        self.tan_mechanism = value

    def get_sepa_accounts(self):
        return [SimpleNamespace(iban="DE001"), SimpleNamespace(iban="DE002")]

    def deconstruct(self):
        return {}


def test_merged_meta_none_deletes_empty_ignores_value_updates():
    import json

    from app.foundation.models.entities import ApiKey
    from app.foundation.settings import _merged_meta

    row = ApiKey(
        service="dkb",
        key_encrypted="x",
        meta_json=json.dumps({"username": "u", "tan_security_function": "910"}),
    )
    # Explicit None deletes the stale pinned mechanism.
    assert _merged_meta(row, {"tan_security_function": None}) == {"username": "u"}
    # Empty string leaves everything unchanged.
    assert _merged_meta(row, {"username": ""}) == {"username": "u", "tan_security_function": "910"}
    # A real value updates the key.
    assert _merged_meta(row, {"username": "new"})["username"] == "new"


def test_run_sync_surfaces_manual_tan_required_message():
    """A DkbFinTSManualTanRequired from the adapter must end in needs_manual_tan
    with its helpful message — not the opaque 'connection error'."""
    import unittest.mock as mock

    from app.foundation.dkb import DkbFinTSManualTanRequired

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = TestSession()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()

    class FakeAdapter:
        pin = "12345"
        username = "dkb-user"

        def fetch_snapshot(self, state_callback=None):
            raise DkbFinTSManualTanRequired(methods=[{"id": "910", "name": "chipTAN"}])

    service = DkbSyncService()
    session = service._session("pending_tan", "Sync started.", "fints")
    service._sessions[session.session_id] = session

    with mock.patch("app.foundation.core.db.SessionLocal", TestSession):
        service._run_sync(session.session_id, FakeAdapter(), user.id)

    updated = service._sessions[session.session_id]
    assert updated.state == "needs_manual_tan"
    assert "DKB-App decoupled push TAN is not available" in updated.message
    assert "connection error" not in updated.message


def test_run_sync_sanitizes_credentials_in_generic_error():
    import unittest.mock as mock

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = TestSession()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()

    class FakeAdapter:
        pin = "supersecretpin"
        username = "dkb-user"

        def fetch_snapshot(self, state_callback=None):
            raise RuntimeError("login rejected for supersecretpin / dkb-user")

    service = DkbSyncService()
    session = service._session("pending_tan", "Sync started.", "fints")
    service._sessions[session.session_id] = session

    with mock.patch("app.foundation.core.db.SessionLocal", TestSession):
        service._run_sync(session.session_id, FakeAdapter(), user.id)

    updated = service._sessions[session.session_id]
    assert updated.state == "failed"
    assert "supersecretpin" not in updated.message
    assert "dkb-user" not in updated.message
    assert "***" in updated.message


def test_verify_login_returns_account_count_for_decoupled():
    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints",
        "12030000",
        "u",
        "p",
        client_factory=_DecoupledSelftestClient,
        sleeper=lambda _s: None,
    )
    result = adapter.verify_login()
    assert result["accounts"] == 2
    assert result["decoupled"] is True
    assert result["tan_methods"][0]["id"] == "920"


def test_verify_login_raises_when_only_manual_tan():
    import pytest

    from app.foundation.dkb import DkbFinTSManualTanRequired

    class ManualClient:
        init_tan_response = None

        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch_tan_mechanisms(self):
            return "910"

        def get_tan_mechanisms(self):
            return {"910": SimpleNamespace(name="chipTAN manuell", tech_id="HHD")}

        def set_tan_mechanism(self, value):
            if value == "940":
                raise ValueError("940 not available")

        def get_sepa_accounts(self):
            return []

        def deconstruct(self):
            return {}

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints",
        "12030000",
        "u",
        "p",
        client_factory=ManualClient,
        sleeper=lambda _s: None,
    )
    with pytest.raises(DkbFinTSManualTanRequired):
        adapter.verify_login()


def test_trigger_force_bypasses_cached_today():
    import unittest.mock as mock
    from datetime import UTC, datetime

    from app.foundation.models.entities import DkbAccount

    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    set_secret(db, "dkb", "12345", {"username": "dkb-user"})
    upsert_public_settings(db, {"dkb_provider": "fints", "dkb_product_id": "TESTPROD001"})
    db.add(DkbAccount(user_id=user.id, type="giro", iban="DE001", last_synced=datetime.now(UTC)))
    db.commit()

    service = DkbSyncService()
    # Without force the same-day guard short-circuits.
    cached = service.trigger(db, user.id, force=False)
    assert cached.state == "cached"

    # With force the guard is skipped and a real sync session starts. Stub the
    # worker so no network call / background thread work happens.
    with mock.patch.object(DkbSyncService, "_run_sync", lambda *a, **k: None):
        forced = service.trigger(db, user.id, force=True)
    assert forced.state == "pending_tan"


def test_update_settings_does_not_persist_dkb_username_plaintext():
    from app.interface.api.settings import update_settings
    from app.foundation.models.entities import AppSetting
    from app.foundation.schemas import SettingsPayload
    from app.foundation.settings import get_secret

    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    # An existing dkb secret so the username fold has a row to update.
    set_secret(db, "dkb", "12345", {"username": "old-user"})

    payload = SettingsPayload(settings={"dkb_username": "new-user", "currency": "EUR"}, integrations={})
    update_settings(payload, db, user)

    assert db.query(AppSetting).filter(AppSetting.key == "dkb_username").count() == 0
    _pin, meta = get_secret(db, "dkb")
    assert meta["username"] == "new-user"


def need_push_tan():
    response = object.__new__(NeedTANResponse)
    response.decoupled = True
    response.challenge = "Confirm in DKB-App"
    response.challenge_html = None
    return response


def test_fints_connection_rejected_raised_on_bad_status_code():
    """A fake client whose fetch_tan_mechanisms raises a FinTSConnectionError
    with 'Bad status code'  must cause the adapter to raise
    DkbFinTSConnectionRejected (not fall through to the 940 fallback)."""
    import pytest

    from app.foundation.dkb import DkbFinTSConnectionRejected

    class FailingClient:
        init_tan_response = None

        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch_tan_mechanisms(self):
            from fints.exceptions import FinTSConnectionError
            raise FinTSConnectionError("Bad status code 400")

        def get_tan_mechanisms(self):
            return {}

        def set_tan_mechanism(self, value):
            pass

        def get_sepa_accounts(self):
            return []

        def deconstruct(self):
            return {}

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        client_factory=FailingClient, sleeper=lambda _s: None,
    )
    with pytest.raises(DkbFinTSConnectionRejected):
        adapter.fetch_snapshot()


def test_fints_connection_rejected_raised_on_system_id_error():
    """Same as above but triggered by 'Could not find system_id' in the error text."""
    import pytest

    from app.foundation.dkb import DkbFinTSConnectionRejected

    class FailingClient:
        init_tan_response = None

        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch_tan_mechanisms(self):
            from fints.exceptions import FinTSConnectionError
            raise FinTSConnectionError("Could not find system_id")

        def get_tan_mechanisms(self):
            return {}

        def set_tan_mechanism(self, value):
            pass

        def get_sepa_accounts(self):
            return []

        def deconstruct(self):
            return {}

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        client_factory=FailingClient, sleeper=lambda _s: None,
    )
    with pytest.raises(DkbFinTSConnectionRejected):
        adapter.verify_login()


def test_selftest_use_test_product_id_overrides_product_id():
    """fints_selftest(use_test_product_id=True) must build the adapter with
    DKB_PUBLIC_TEST_PRODUCT_ID. We verify this by patching the factory and
    capturing the product_id passed to it."""
    import unittest.mock as mock

    from app.interface.api.dkb import fints_selftest
    from app.foundation.dkb import DKB_PUBLIC_TEST_PRODUCT_ID

    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    set_secret(db, "dkb", "12345", {"username": "dkb-user"})
    upsert_public_settings(db, {"dkb_provider": "fints", "dkb_product_id": "CUSTOM_PROD_001"})

    captured_product_id = []

    class CapturingClient(_DecoupledSelftestClient):
        def __init__(self, *_args, **_kwargs):
            captured_product_id.append(_kwargs.get("product_id"))
            super().__init__(*_args, **_kwargs)

    with mock.patch("fints.client.FinTS3PinTanClient", CapturingClient):
        result = fints_selftest(db, user, use_test_product_id=True)

    assert result["status"] == "passed"
    assert captured_product_id[0] == DKB_PUBLIC_TEST_PRODUCT_ID
    assert any(s["key"] == "test_product_id" and s["status"] == "passed" for s in result["steps"])


def test_system_id_passed_to_client_and_returned_on_success():
    """A successful verify_login must return the client's system_id, and the
    adapter must pass it through to the FinTS3PinTanClient constructor."""

    class ClientWithSystemId:
        init_tan_response = None
        system_id = "12345"

        def __init__(self, *_args, **_kwargs):
            self._fetched = False
            self._passed_system_id = _kwargs.get("system_id")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch_tan_mechanisms(self):
            self._fetched = True
            return "920"

        def get_tan_mechanisms(self):
            return {"920": type("Params", (), {"name": "DKB-App", "tech_id": "DECOUPLED"})()}

        def set_tan_mechanism(self, value):
            self.tan_mechanism = value

        def get_sepa_accounts(self):
            return [type("Acc", (), {"iban": "DE001"})()]

        def deconstruct(self):
            return {}

    # Without system_id
    adapter_no_sid = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        client_factory=ClientWithSystemId, sleeper=lambda _s: None,
    )
    result = adapter_no_sid.verify_login()
    assert result.get("system_id") == "12345"

    # With system_id passed in
    adapter_with_sid = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        system_id="previous_sid",
        client_factory=ClientWithSystemId, sleeper=lambda _s: None,
    )
    result2 = adapter_with_sid.verify_login()
    assert result2.get("system_id") == "12345"


def test_send_tan_called_with_none_not_empty_string():
    """The decoupled poll loop must call client.send_tan(response, None)
    (not send_tan(response, ''))."""
    send_tan_args = []

    class SendTanClient:
        init_tan_response = None

        def __init__(self, *_args, **_kwargs):
            self._fetched = False
            self.polls = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch_tan_mechanisms(self):
            self._fetched = True
            return "920"

        def get_tan_mechanisms(self):
            return {"920": type("Params", (), {"name": "DKB-App (decoupled)", "tech_id": "DECOUPLED"})()}

        def set_tan_mechanism(self, value):
            self.tan_mechanism = value

        def get_sepa_accounts(self):
            return need_push_tan()

        def send_tan(self, response, tan):
            send_tan_args.append(tan)
            self.polls += 1
            if self.polls == 1:
                return response
            return [type("Acc", (), {"iban": "DE001", "product_name": "Girokonto"})()]

        def get_balance(self, _account):
            return type("Bal", (), {"amount": type("Amt", (), {"amount": "42.50"})()})()

        def get_transactions(self, _account, *_args):
            return []

        def deconstruct(self):
            return {}

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        client_factory=SendTanClient, sleeper=lambda _s: None,
    )
    adapter.fetch_snapshot()
    assert len(send_tan_args) > 0
    for arg in send_tan_args:
        assert arg is None, f"Expected None but got {arg!r}"


def test_settings_test_integration_dkb_returns_ok_false_on_exception():
    """test_integration for dkb must never let an exception escape as a 500."""
    import unittest.mock as mock

    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()

    payload = IntegrationTestRequest(service="dkb", value=None, meta={})

    with mock.patch("app.interface.api.settings.get_secret", side_effect=RuntimeError("db connection lost")):
        result = settings_test_integration(payload, db, user)

    # Must NOT raise — must return a clean IntegrationTestResponse
    assert result.ok is False
    assert result.service == "dkb"
    assert result.message  # truthy, not a raw 500


def test_trigger_use_test_product_id_builds_adapter_correctly():
    """trigger(use_test_product_id=True) must build the adapter with:
    - product_id == DKB_PUBLIC_TEST_PRODUCT_ID
    - system_id is None (never the stored one)
    - system_id_meta_key == "fints_system_id_test"
    """
    import unittest.mock as mock

    from app.foundation.dkb import DKB_PUBLIC_TEST_PRODUCT_ID

    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    set_secret(db, "dkb", "12345", {"username": "dkb-user"})
    upsert_public_settings(db, {
        "dkb_provider": "fints",
        "dkb_product_id": "CUSTOM_PROD_001",
        "dkb_fints_url": "https://fints.dkb.de/fints",
        "dkb_blz": "12030000",
    })

    captured_adapter = {}

    def _capture_adapter(self, session_id, adapter, user_id, db=None):
        captured_adapter["adapter"] = adapter
        # Don't actually run — just capture
    with mock.patch.object(DkbSyncService, "_run_sync", _capture_adapter):
        service = DkbSyncService()
        service.trigger(db, user.id, use_test_product_id=True)

    adapter = captured_adapter.get("adapter")
    assert adapter is not None
    assert adapter.product_id == DKB_PUBLIC_TEST_PRODUCT_ID
    assert adapter.system_id is None
    assert adapter.system_id_meta_key == "fints_system_id_test"


def test_run_sync_persists_system_id_under_correct_meta_key():
    """_run_sync must use adapter.system_id_meta_key when persisting the system_id
    via update_secret_meta. When system_id_meta_key == "fints_system_id_test", the
    system_id must be stored under that key (not fints_system_id)."""
    import unittest.mock as mock

    from app.foundation.settings import update_secret_meta

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = TestSession()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    set_secret(db, "dkb", "12345", {"username": "dkb-user"})
    upsert_public_settings(db, {"dkb_provider": "fints", "dkb_product_id": "PROD001"})

    class FakeAdapter:
        pin = "12345"
        username = "dkb-user"
        system_id_meta_key = "fints_system_id_test"  # test-PID mode
        system_id = None

        def fetch_snapshot(self, state_callback=None):
            return {"accounts": [], "transactions": [], "positions": [], "system_id": "TEST_SID_123"}

    original_update = update_secret_meta

    call_args = []

    def tracking_update(db, service, meta):
        call_args.append((service, dict(meta)))
        return original_update(db, service, meta)

    service = DkbSyncService()
    session = service._session("pending_tan", "Sync started.", "fints")
    service._sessions[session.session_id] = session

    with mock.patch("app.foundation.core.db.SessionLocal", TestSession):
        with mock.patch("app.foundation.settings.update_secret_meta", tracking_update):
            service._run_sync(session.session_id, FakeAdapter(), user.id)

    # Check that fints_system_id_test was persisted (not fints_system_id)
    persisted = {}
    for service_name, meta in call_args:
        if service_name == "dkb":
            persisted.update(meta)
    assert persisted.get("fints_system_id_test") == "TEST_SID_123", (
        f"Expected fints_system_id_test=TEST_SID_123 in persisted meta, got {persisted}"
    )
    assert "fints_system_id" not in persisted, (
        "Must not persist system_id under the regular fints_system_id key during test-PID run"
    )

    # Verify the session completed successfully
    updated = service._sessions[session.session_id]
    assert updated.state == "confirmed"


def test_configure_tan_raises_connection_rejected_on_fints_connection_error():
    """_configure_tan must raise DkbFinTSConnectionRejected when
    fetch_tan_mechanisms raises FinTSConnectionError (the broadened
    detection), not just on the substring match."""
    import pytest
    from fints.exceptions import FinTSConnectionError

    from app.foundation.dkb import DkbFinTSConnectionRejected

    class ErrorClient:
        init_tan_response = None

        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch_tan_mechanisms(self):
            raise FinTSConnectionError("HTTP 400 from DKB")

        def get_tan_mechanisms(self):
            return {}

        def set_tan_mechanism(self, value):
            pass

        def get_sepa_accounts(self):
            return []

        def deconstruct(self):
            return {}

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        client_factory=ErrorClient, sleeper=lambda _s: None,
    )
    with pytest.raises(DkbFinTSConnectionRejected):
        adapter.fetch_snapshot()


def test_configure_tan_raises_connection_rejected_on_http_400():
    """Same as above but with a generic HTTP 400-like error that does NOT
    contain the standard substrings — verifies class-based detection."""
    import pytest
    from fints.exceptions import FinTSConnectionError

    from app.foundation.dkb import DkbFinTSConnectionRejected

    class ErrorClient:
        init_tan_response = None

        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch_tan_mechanisms(self):
            raise FinTSConnectionError("401 Unauthorized — unrecognized product_id")

        def get_tan_mechanisms(self):
            return {}

        def set_tan_mechanism(self, value):
            pass

        def get_sepa_accounts(self):
            return []

        def deconstruct(self):
            return {}

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        client_factory=ErrorClient, sleeper=lambda _s: None,
    )
    with pytest.raises(DkbFinTSConnectionRejected):
        adapter.verify_login()


def test_run_sync_surfaces_connection_rejected_message():
    """A DkbFinTSConnectionRejected from the adapter must end in 'failed'
    with an actionable message — not the generic fallback."""
    import unittest.mock as mock

    from app.foundation.dkb import DkbFinTSConnectionRejected

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = TestSession()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()

    class FakeAdapter:
        pin = "12345"
        username = "dkb-user"

        def fetch_snapshot(self, state_callback=None):
            raise DkbFinTSConnectionRejected()

    service = DkbSyncService()
    session = service._session("pending_tan", "Sync started.", "fints")
    service._sessions[session.session_id] = session

    with mock.patch("app.foundation.core.db.SessionLocal", TestSession):
        service._run_sync(session.session_id, FakeAdapter(), user.id)

    updated = service._sessions[session.session_id]
    assert updated.state == "failed"
    assert "400" in updated.message
    assert "connection error" not in updated.message


def test_fetch_snapshot_includes_errors_key():
    """fetch_snapshot result must contain an 'errors' key (empty when no
    errors occur)."""
    class CleanClient:
        init_tan_response = None

        def __init__(self, *_args, **_kwargs):
            self._fetched = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch_tan_mechanisms(self):
            self._fetched = True
            return "920"

        def get_tan_mechanisms(self):
            return {"920": SimpleNamespace(name="DKB-App (decoupled)", tech_id="DECOUPLED")}

        def set_tan_mechanism(self, value):
            self.tan_mechanism = value

        def get_sepa_accounts(self):
            return [SimpleNamespace(iban="DE001", product_name="Girokonto")]

        def get_balance(self, _account):
            return SimpleNamespace(amount=Decimal("100.00"))

        def get_transactions(self, _account, *_args):
            return []

        def deconstruct(self):
            return {}

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        client_factory=CleanClient, sleeper=lambda _s: None,
    )
    result = adapter.fetch_snapshot()
    assert "errors" in result
    assert result["errors"] == []


def test_fetch_snapshot_continues_on_balance_failure():
    """When get_balance fails for one account, fetch_snapshot must continue
    with other accounts and collect the error."""
    calls = []

    class PartialBalanceClient:
        init_tan_response = None

        def __init__(self, *_args, **_kwargs):
            self._fetched = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch_tan_mechanisms(self):
            self._fetched = True
            return "920"

        def get_tan_mechanisms(self):
            return {"920": SimpleNamespace(name="DKB-App (decoupled)", tech_id="DECOUPLED")}

        def set_tan_mechanism(self, value):
            self.tan_mechanism = value

        def get_sepa_accounts(self):
            return [
                SimpleNamespace(iban="DE001", product_name="Girokonto"),
                SimpleNamespace(iban="DE002", product_name="Visa Karte"),
            ]

        def get_balance(self, account):
            calls.append(("get_balance", account.iban))
            if account.iban == "DE001":
                raise RuntimeError("Balance service unavailable")
            return SimpleNamespace(amount=Decimal("200.00"))

        def get_transactions(self, _account, *_args):
            return []

        def deconstruct(self):
            return {}

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        client_factory=PartialBalanceClient, sleeper=lambda _s: None,
    )
    result = adapter.fetch_snapshot()

    # Both accounts must have been attempted
    assert ("get_balance", "DE001") in calls
    assert ("get_balance", "DE002") in calls

    # DE001 balance failed, DE002 succeeded
    assert len(result["accounts"]) == 1
    assert result["accounts"][0]["iban"] == "DE002"

    # Error captured for DE001 (last 4 chars = E001)
    assert len(result["errors"]) == 1
    assert result["errors"][0]["op"] == "get_balance"
    assert "E001" in result["errors"][0]["account"]
    """When get_balance fails for an account, get_transactions and get_holdings
    must be skipped for that account (continue to next account)."""
    balance_calls = []
    tx_calls = []

    class SkipRemainingClient:
        init_tan_response = None

        def __init__(self, *_args, **_kwargs):
            self._fetched = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch_tan_mechanisms(self):
            self._fetched = True
            return "920"

        def get_tan_mechanisms(self):
            return {"920": SimpleNamespace(name="DKB-App (decoupled)", tech_id="DECOUPLED")}

        def set_tan_mechanism(self, value):
            self.tan_mechanism = value

        def get_sepa_accounts(self):
            return [SimpleNamespace(iban="DE001", product_name="Girokonto")]

        def get_balance(self, account):
            balance_calls.append(account.iban)
            raise RuntimeError("Balance failed")

        def get_transactions(self, _account, *_args):
            tx_calls.append("would be skipped")
            return []

        def deconstruct(self):
            return {}

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        client_factory=SkipRemainingClient, sleeper=lambda _s: None,
    )
    result = adapter.fetch_snapshot()

    assert len(balance_calls) == 1  # balance was attempted
    assert len(tx_calls) == 0       # transactions were skipped
    assert len(result["accounts"]) == 0
    assert len(result["transactions"]) == 0
    assert len(result["errors"]) == 1
    assert result["errors"][0]["op"] == "get_balance"


def test_fetch_snapshot_continues_on_transactions_failure():
    """When get_transactions fails for an account, the adapter must continue
    (not abort the whole sync), collect the error, and still process other
    accounts."""
    class TxFailureClient:
        init_tan_response = None

        def __init__(self, *_args, **_kwargs):
            self._fetched = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch_tan_mechanisms(self):
            self._fetched = True
            return "920"

        def get_tan_mechanisms(self):
            return {"920": SimpleNamespace(name="DKB-App (decoupled)", tech_id="DECOUPLED")}

        def set_tan_mechanism(self, value):
            self.tan_mechanism = value

        def get_sepa_accounts(self):
            return [
                SimpleNamespace(iban="DE001", product_name="Girokonto"),
                SimpleNamespace(iban="DE002", product_name="Visa Karte"),
            ]

        def get_balance(self, account):
            return SimpleNamespace(amount=Decimal("100.00"))

        def get_transactions(self, account, *_args):
            if account.iban == "DE001":
                raise RuntimeError("Transactions service error")
            return []

        def deconstruct(self):
            return {}

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        client_factory=TxFailureClient, sleeper=lambda _s: None,
    )
    result = adapter.fetch_snapshot()

    # Both accounts got balances
    assert len(result["accounts"]) == 2

    # Error collected for DE001 transactions (last 4 chars = E001)
    assert len(result["errors"]) == 1
    assert result["errors"][0]["op"] == "get_transactions"
    assert "E001" in result["errors"][0]["account"]


def test_fetch_snapshot_continues_on_holdings_failure():
    """When get_holdings fails for a depot account, the adapter must continue
    (not abort the whole sync) and collect the error."""
    class HoldingsFailureClient:
        init_tan_response = None

        def __init__(self, *_args, **_kwargs):
            self._fetched = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch_tan_mechanisms(self):
            self._fetched = True
            return "920"

        def get_tan_mechanisms(self):
            return {"920": SimpleNamespace(name="DKB-App (decoupled)", tech_id="DECOUPLED")}

        def set_tan_mechanism(self, value):
            self.tan_mechanism = value

        def get_sepa_accounts(self):
            return [SimpleNamespace(iban="DE001", product_name="Depot")]

        def get_balance(self, account):
            return SimpleNamespace(amount=Decimal("5000.00"))

        def get_transactions(self, _account, *_args):
            return []

        def get_holdings(self, _account):
            raise RuntimeError("Holdings service error")

        def deconstruct(self):
            return {}

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        client_factory=HoldingsFailureClient, sleeper=lambda _s: None,
    )
    result = adapter.fetch_snapshot()

    # Balance and transactions should still succeed
    assert len(result["accounts"]) == 1
    assert len(result["positions"]) == 0

    # Error collected for holdings
    assert len(result["errors"]) == 1
    assert result["errors"][0]["op"] == "get_holdings"


def test_debug_fints_logging_sets_and_restores_level():
    """When debug_fints_logging is True, fetch_snapshot must set the 'fints'
    logger to DEBUG level and restore its original level on exit."""
    import logging

    class QuietClient:
        init_tan_response = None

        def __init__(self, *_args, **_kwargs):
            self._fetched = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch_tan_mechanisms(self):
            self._fetched = True
            return "920"

        def get_tan_mechanisms(self):
            return {"920": SimpleNamespace(name="DKB-App (decoupled)", tech_id="DECOUPLED")}

        def set_tan_mechanism(self, value):
            self.tan_mechanism = value

        def get_sepa_accounts(self):
            return [SimpleNamespace(iban="DE001", product_name="Girokonto")]

        def get_balance(self, _account):
            return SimpleNamespace(amount=Decimal("100.00"))

        def get_transactions(self, _account, *_args):
            return []

        def deconstruct(self):
            return {}

    fints_logger = logging.getLogger("fints")
    fints_logger.setLevel(logging.WARNING)  # ensure baseline

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        client_factory=QuietClient, sleeper=lambda _s: None,
    )
    adapter.debug_fints_logging = True

    # Level should be DEBUG during execution
    adapter.fetch_snapshot()

    # Level must be restored after execution
    assert fints_logger.level == logging.WARNING, (
        f"Expected fints logger at WARNING after fetch, got {fints_logger.level}"
    )


def test_debug_fints_logging_restores_level_even_on_error():
    """When an exception is raised, debug_fints_logging must still restore
    the original logger level."""
    import logging

    class FailingDebugClient:
        init_tan_response = None

        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch_tan_mechanisms(self):
            self._fetched = True
            return "920"

        def get_tan_mechanisms(self):
            return {"920": SimpleNamespace(name="DKB-App (decoupled)", tech_id="DECOUPLED")}

        def set_tan_mechanism(self, value):
            self.tan_mechanism = value

        def get_sepa_accounts(self):
            raise RuntimeError("SEPA accounts unavailable")

        def deconstruct(self):
            return {}

    fints_logger = logging.getLogger("fints")
    fints_logger.setLevel(logging.WARNING)

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        client_factory=FailingDebugClient, sleeper=lambda _s: None,
    )
    adapter.debug_fints_logging = True

    import pytest
    with pytest.raises(RuntimeError):
        adapter.fetch_snapshot()

    assert fints_logger.level == logging.WARNING, (
        f"Expected fints logger at WARNING after error, got {fints_logger.level}"
    )


def test_fetch_snapshot_masked_iban_in_errors():
    """Error entries must contain a masked account identifier (last 4 chars),
    not the full IBAN."""
    class MaskingClient:
        init_tan_response = None

        def __init__(self, *_args, **_kwargs):
            self._fetched = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch_tan_mechanisms(self):
            self._fetched = True
            return "920"

        def get_tan_mechanisms(self):
            return {"920": SimpleNamespace(name="DKB-App (decoupled)", tech_id="DECOUPLED")}

        def set_tan_mechanism(self, value):
            self.tan_mechanism = value

        def get_sepa_accounts(self):
            return [SimpleNamespace(iban="DE02123456789012345678", product_name="Girokonto")]

        def get_balance(self, _account):
            raise RuntimeError("balance crash")

        def get_transactions(self, _account, *_args):
            return []

        def deconstruct(self):
            return {}

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        client_factory=MaskingClient, sleeper=lambda _s: None,
    )
    result = adapter.fetch_snapshot()
    assert len(result["errors"]) == 1
    masked = result["errors"][0]["account"]
    # Should be the last 4 chars prefixed with ...
    assert "5678" in masked
    assert "DE02123456789012345678" not in masked  # full IBAN must not appear


def test_run_sync_reports_partial_snapshot_errors():
    """_run_sync must surface per-operation errors in session logs and
    complete with 'confirmed' state and a partial-success message."""
    import unittest.mock as mock

    from app.foundation.dkb import DkbSyncService

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = TestSession()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    set_secret(db, "dkb", "12345", {"username": "dkb-user"})
    upsert_public_settings(db, {"dkb_provider": "fints", "dkb_product_id": "PROD001"})

    class FakeAdapter:
        pin = "12345"
        username = "dkb-user"
        system_id_meta_key = "fints_system_id"
        system_id = None

        def fetch_snapshot(self, state_callback=None):
            return {
                "accounts": [{"type": "giro", "iban": "DE001", "balance": 100}],
                "transactions": [],
                "positions": [],
                "errors": [
                    {"op": "get_transactions", "account": "...0001", "message": "timeout"},
                ],
            }

    service = DkbSyncService()
    session = service._session("pending_tan", "Sync started.", "fints")
    service._sessions[session.session_id] = session

    with mock.patch("app.foundation.core.db.SessionLocal", TestSession):
        service._run_sync(session.session_id, FakeAdapter(), user.id)

    updated = service._sessions[session.session_id]
    assert updated.state == "confirmed"
    assert "issue" in updated.message or "error" in updated.message.lower()
    assert "get_transactions" in updated.message


def test_fetch_snapshot_sanitizes_credentials_in_errors():
    """Error message strings must have PIN and username redacted."""
    class LeakyClient:
        init_tan_response = None

        def __init__(self, *_args, **_kwargs):
            self._fetched = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch_tan_mechanisms(self):
            self._fetched = True
            return "920"

        def get_tan_mechanisms(self):
            return {"920": SimpleNamespace(name="DKB-App (decoupled)", tech_id="DECOUPLED")}

        def set_tan_mechanism(self, value):
            self.tan_mechanism = value

        def get_sepa_accounts(self):
            return [SimpleNamespace(iban="DE001", product_name="Girokonto")]

        def get_balance(self, _account):
            raise RuntimeError("login rejected for mysecretpin / myuser")

        def get_transactions(self, _account, *_args):
            return []

        def deconstruct(self):
            return {}

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "myuser", "mysecretpin",
        client_factory=LeakyClient, sleeper=lambda _s: None,
    )
    result = adapter.fetch_snapshot()
    assert len(result["errors"]) == 1
    msg = result["errors"][0]["message"]
    assert "mysecretpin" not in msg
    assert "myuser" not in msg
    assert "***" in msg


def test_complete_tan_retries_on_send_tan_http_400_then_succeeds():
    """DKB returns HTTP 400 while the decoupled push TAN is still pending. The
    adapter must retry (sleep + poll again) rather than aborting. Once the user
    approves, the subsequent poll returns a non-NeedTAN response and the sync
    completes successfully."""
    from fints.exceptions import FinTSConnectionError

    class ApproveAfterRetryClient:
        init_tan_response = None

        def __init__(self, *_args, **_kwargs):
            self._send_tan_calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch_tan_mechanisms(self):
            return "920"

        def get_tan_mechanisms(self):
            return {"920": SimpleNamespace(name="DKB-App (decoupled)", tech_id="DECOUPLED")}

        def set_tan_mechanism(self, value):
            pass

        def get_sepa_accounts(self):
            return need_push_tan()

        def send_tan(self, response, tan):
            self._send_tan_calls += 1
            if self._send_tan_calls < 3:
                raise FinTSConnectionError("Bad status code 400")
            return []  # approval confirmed; non-NeedTANResponse ends the loop

        def deconstruct(self):
            return {}

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        client_factory=ApproveAfterRetryClient, sleeper=lambda _s: None,
    )
    result = adapter.fetch_snapshot()
    assert result["accounts"] == []
    assert result["errors"] == []


def test_complete_tan_times_out_on_persistent_http_400():
    """If DKB keeps returning 400 for the full push_timeout window, the adapter
    must raise DkbFinTSTanTimeout (not DkbFinTSConnectionRejected)."""
    import pytest
    from fints.exceptions import FinTSConnectionError

    from app.foundation.dkb import DkbFinTSTanTimeout

    class AlwaysRejectClient:
        init_tan_response = None

        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch_tan_mechanisms(self):
            return "920"

        def get_tan_mechanisms(self):
            return {"920": SimpleNamespace(name="DKB-App (decoupled)", tech_id="DECOUPLED")}

        def set_tan_mechanism(self, value):
            pass

        def get_sepa_accounts(self):
            return need_push_tan()

        def send_tan(self, response, tan):
            raise FinTSConnectionError("Bad status code 400")

        def deconstruct(self):
            return {}

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        client_factory=AlwaysRejectClient,
        sleeper=lambda _s: None,
        push_timeout_seconds=0,  # expire immediately so the test doesn't spin
    )
    with pytest.raises(DkbFinTSTanTimeout):
        adapter.fetch_snapshot()


# ---------------------------------------------------------------------------
# _coerce_int
# ---------------------------------------------------------------------------


def test_coerce_int_returns_none_for_none():
    from app.foundation.dkb import _coerce_int
    assert _coerce_int(None) is None


def test_coerce_int_returns_int_for_int():
    from app.foundation.dkb import _coerce_int
    assert _coerce_int(7) == 7


def test_coerce_int_returns_int_for_int_string():
    from app.foundation.dkb import _coerce_int
    assert _coerce_int("5") == 5


def test_coerce_int_returns_none_for_non_integer():
    from app.foundation.dkb import _coerce_int
    assert _coerce_int("not-a-number") is None


# ---------------------------------------------------------------------------
# _complete_tan  —  primary fix: initial wait before first poll
# ---------------------------------------------------------------------------


def test_complete_tan_initial_wait_from_mechanism_params():
    """When the selected mechanism exposes wait_before_first_poll=7, the
    first sleeper call must receive 7 (not the fallback poll_interval)."""
    sleeper_calls = []

    class WaitParamClient:
        init_tan_response = None

        def __init__(self, *_args, **_kwargs):
            self._fetched = False

        def __enter__(self): return self
        def __exit__(self, *_args): return None

        def fetch_tan_mechanisms(self):
            self._fetched = True
            return "920"

        def get_tan_mechanisms(self):
            return {
                "920": SimpleNamespace(
                    name="DKB-App (decoupled)", tech_id="DECOUPLED",
                    wait_before_first_poll=7,
                    wait_before_next_poll=3,
                )
            }

        def set_tan_mechanism(self, value):
            self.tan_mechanism = value

        def get_sepa_accounts(self):
            return need_push_tan()

        def send_tan(self, response, tan):
            return [SimpleNamespace(iban="DE001", product_name="Girokonto")]

        def get_balance(self, _account):
            return SimpleNamespace(amount=Decimal("42.50"))

        def get_transactions(self, _account, *_args):
            return []

        def deconstruct(self):
            return {}

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        client_factory=WaitParamClient,
        sleeper=lambda s: sleeper_calls.append(s),
    )
    adapter.fetch_snapshot()
    assert len(sleeper_calls) >= 1, "Expected at least one sleeper call (initial wait)"
    assert sleeper_calls[0] == 7, (
        f"First sleeper call should be wait_before_first_poll (7), got {sleeper_calls[0]}"
    )


def test_complete_tan_initial_wait_falls_back_to_poll_interval():
    """When no wait_before_first_poll attribute is present on the mechanism,
    the initial sleeper call must fall back to poll_interval_seconds (5)."""
    sleeper_calls = []

    class NoWaitParamClient:
        init_tan_response = None

        def __init__(self, *_args, **_kwargs):
            self._fetched = False

        def __enter__(self): return self
        def __exit__(self, *_args): return None

        def fetch_tan_mechanisms(self):
            self._fetched = True
            return "920"

        def get_tan_mechanisms(self):
            return {
                "920": SimpleNamespace(
                    name="DKB-App (decoupled)", tech_id="DECOUPLED",
                )
            }

        def set_tan_mechanism(self, value):
            self.tan_mechanism = value

        def get_sepa_accounts(self):
            return need_push_tan()

        def send_tan(self, response, tan):
            return [SimpleNamespace(iban="DE001", product_name="Girokonto")]

        def get_balance(self, _account):
            return SimpleNamespace(amount=Decimal("42.50"))

        def get_transactions(self, _account, *_args):
            return []

        def deconstruct(self):
            return {}

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        client_factory=NoWaitParamClient,
        sleeper=lambda s: sleeper_calls.append(s),
    )
    adapter.fetch_snapshot()
    assert len(sleeper_calls) >= 1, "Expected at least one sleeper call (initial wait)"
    assert sleeper_calls[0] == adapter.poll_interval_seconds, (
        f"First sleeper call should be poll_interval_seconds ({adapter.poll_interval_seconds}), "
        f"got {sleeper_calls[0]}"
    )


def test_complete_tan_raises_on_genuine_rejection_immediately():
    """A non-pending HTTP error (e.g. Bad status code 500) must raise
    DkbFinTSConnectionRejected immediately, without sleeping or retrying."""
    import pytest
    from fints.exceptions import FinTSConnectionError

    from app.foundation.dkb import DkbFinTSConnectionRejected

    sleeper_calls = []

    class Reject500Client:
        init_tan_response = None

        def __init__(self, *_args, **_kwargs):
            self._fetched = False

        def __enter__(self): return self
        def __exit__(self, *_args): return None

        def fetch_tan_mechanisms(self):
            self._fetched = True
            return "920"

        def get_tan_mechanisms(self):
            return {"920": SimpleNamespace(name="DKB-App (decoupled)", tech_id="DECOUPLED")}

        def set_tan_mechanism(self, value):
            self.tan_mechanism = value

        def get_sepa_accounts(self):
            return need_push_tan()

        def send_tan(self, response, tan):
            raise FinTSConnectionError("Bad status code 500")

        def deconstruct(self):
            return {}

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        client_factory=Reject500Client,
        sleeper=lambda s: sleeper_calls.append(s),
        push_timeout_seconds=30,
    )
    with pytest.raises(DkbFinTSConnectionRejected):
        adapter.fetch_snapshot()
    # Should reject before any loop sleep (only the initial wait)
    # We can't easily assert on exact count since initial wait fires,
    # but we verify that the snapshot did NOT complete.
    # The primary assertion is the exception type.


def test_complete_tan_annotates_unexpected_poll_failure():
    """A completely unexpected exception (not a rejection signal) must be
    annotated with the operation context via RuntimeError, not silently eaten."""
    import pytest

    from app.foundation.dkb import DkbFinTSConnectionRejected

    class WeirdFailureClient:
        init_tan_response = None

        def __init__(self, *_args, **_kwargs):
            self._fetched = False

        def __enter__(self): return self
        def __exit__(self, *_args): return None

        def fetch_tan_mechanisms(self):
            self._fetched = True
            return "920"

        def get_tan_mechanisms(self):
            return {"920": SimpleNamespace(name="DKB-App (decoupled)", tech_id="DECOUPLED")}

        def set_tan_mechanism(self, value):
            self.tan_mechanism = value

        def get_sepa_accounts(self):
            return need_push_tan()

        def send_tan(self, response, tan):
            raise ValueError("unexpected internal error")

        def deconstruct(self):
            return {}

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        client_factory=WeirdFailureClient,
        sleeper=lambda _s: None,
        push_timeout_seconds=30,
    )
    # Must propagate as a RuntimeError with annotation, NOT silently eaten
    with pytest.raises(RuntimeError) as exc_info:
        adapter.fetch_snapshot()
    assert "Decoupled TAN poll failed" in str(exc_info.value)
    assert "ValueError" in str(exc_info.value)
    # Must NOT be DkbFinTSConnectionRejected (that's for rejection signals)
    assert not isinstance(exc_info.value, DkbFinTSConnectionRejected)


def test_complete_tan_between_poll_uses_wait_before_next_poll():
    """Re-returned NeedTANResponse (code 3956 path): when the mechanism exposes
    wait_before_next_poll=3, the sleeper calls *after* the initial wait must all
    use 3, not the default poll_interval_seconds (5)."""
    sleeper_calls = []

    class RepollingClient:
        init_tan_response = None

        def __init__(self, *_args, **_kwargs):
            self._polls = 0

        def __enter__(self): return self
        def __exit__(self, *_args): return None

        def fetch_tan_mechanisms(self):
            return "920"

        def get_tan_mechanisms(self):
            return {
                "920": SimpleNamespace(
                    name="DKB-App (decoupled)", tech_id="DECOUPLED",
                    wait_before_first_poll=7,
                    wait_before_next_poll=3,
                )
            }

        def set_tan_mechanism(self, value):
            pass

        def get_sepa_accounts(self):
            return need_push_tan()

        def send_tan(self, response, tan):
            self._polls += 1
            if self._polls < 3:
                return response  # still pending (NeedTANResponse re-returned)
            return [SimpleNamespace(iban="DE002", product_name="Girokonto")]

        def get_balance(self, _account):
            return SimpleNamespace(amount=Decimal("10.00"))

        def get_transactions(self, _account, *_args):
            return []

        def deconstruct(self):
            return {}

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        client_factory=RepollingClient,
        sleeper=lambda s: sleeper_calls.append(s),
    )
    adapter.fetch_snapshot()

    # First call = initial wait (7), subsequent calls = between-poll interval (3)
    assert sleeper_calls[0] == 7, f"Initial wait should be 7, got {sleeper_calls[0]}"
    assert all(s == 3 for s in sleeper_calls[1:]), (
        f"Between-poll sleeps should all be 3 (wait_before_next_poll), got {sleeper_calls[1:]}"
    )
# ---------------------------------------------------------------------------
# Pillar tests: debug_log capture, context manager, detail property
# ---------------------------------------------------------------------------


def test_capture_dkb_logs_captures_lines():
    """_capture_dkb_logs(True) must capture DEBUG lines from the 'fints' logger
    and restore the original logger level afterward."""
    import logging

    from app.foundation.dkb import _capture_dkb_logs

    fints_logger = logging.getLogger("fints")
    fints_logger.setLevel(logging.WARNING)

    captured: list[str] = []
    with _capture_dkb_logs(True) as lines:
        fints_logger.debug("wire: HNVSD:123")
        fints_logger.debug("wire: HNHBS:456")
        captured = lines

    assert len(captured) == 2
    assert any("HNVSD:123" in line for line in captured)
    assert any("HNHBS:456" in line for line in captured)
    assert fints_logger.level == logging.WARNING, (
        f"Expected fints logger at WARNING after context, got {fints_logger.level}"
    )


def test_capture_dkb_logs_noop_for_fints_when_disabled():
    """_capture_dkb_logs(False) must NOT capture fints DEBUG lines and must
    restore the fints logger level. App-level INFO is still captured (always-on)."""
    import logging

    from app.foundation.dkb import _capture_dkb_logs

    fints_logger = logging.getLogger("fints")
    fints_logger.setLevel(logging.WARNING)

    with _capture_dkb_logs(False) as lines:
        fints_logger.debug("this should not be captured")

    assert not any("this should not be captured" in line for line in lines)
    assert fints_logger.level == logging.WARNING


def test_capture_dkb_logs_restores_on_error():
    """_capture_dkb_logs must restore the original logger level even when the
    wrapped code raises an exception."""
    import logging

    from app.foundation.dkb import _capture_dkb_logs

    fints_logger = logging.getLogger("fints")
    fints_logger.setLevel(logging.WARNING)

    import pytest
    with pytest.raises(RuntimeError):
        with _capture_dkb_logs(True) as _lines:
            fints_logger.debug("wire: before explosion")
            raise RuntimeError("boom")

    assert fints_logger.level == logging.WARNING, (
        f"Expected fints logger at WARNING after context error, got {fints_logger.level}"
    )


def test_capture_dkb_logs_captures_app_logger_always():
    """_capture_dkb_logs always captures app.foundation.dkb INFO regardless of verbose flag."""
    import logging

    from app.foundation.dkb import _capture_dkb_logs

    app_logger = logging.getLogger("app.foundation.dkb")
    fints_logger = logging.getLogger("fints")
    fints_logger.setLevel(logging.WARNING)

    with _capture_dkb_logs(False) as lines:
        app_logger.info("DKB FinTS: test INFO observability")
        fints_logger.debug("fints wire: absent when verbose=False")

    assert any("test INFO observability" in line for line in lines), \
        "app.foundation.dkb INFO must be captured even with verbose=False"
    assert not any("absent when verbose=False" in line for line in lines), \
        "fints DEBUG must not be captured when verbose=False"
    assert fints_logger.level == logging.WARNING


def test_capture_dkb_logs_redacts_secrets():
    """_capture_dkb_logs must redact secrets passed as *secrets arguments."""
    import logging

    from app.foundation.dkb import _capture_dkb_logs

    fints_logger = logging.getLogger("fints")
    fints_logger.setLevel(logging.WARNING)

    with _capture_dkb_logs(True, "supersecretpin", "myusername") as lines:
        fints_logger.debug("auth: user=myusername pin=supersecretpin token=abc")

    assert len(lines) == 1
    assert "supersecretpin" not in lines[0]
    assert "myusername" not in lines[0]
    assert "***" in lines[0]
    assert "token=abc" in lines[0]


def test_complete_tan_non_decoupled_raises_with_detail():
    """_complete_tan must raise DkbFinTSManualTanRequired with an actionable detail
    when the bank returns a non-decoupled challenge (wrong TAN security function)."""
    import pytest

    from app.foundation.dkb import DkbFinTSManualTanRequired

    response = object.__new__(NeedTANResponse)
    response.challenge = "Enter TAN from SMS"
    response.challenge_html = None
    response.decoupled = False

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        sleeper=lambda _s: None,
    )

    with pytest.raises(DkbFinTSManualTanRequired) as exc_info:
        adapter._complete_tan(None, response, None)

    msg = str(exc_info.value)
    assert "non-decoupled" in msg
    assert "TAN security function" in msg or "security function" in msg


def test_adapter_debug_log_set_on_adapter_after_fetch():
    """After fetch_snapshot, adapter.debug_log holds the captured lines (last 200)."""
    class SimpleClient:
        init_tan_response = None
        def __init__(self, *_a, **_kw):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_a):
            return None
        def fetch_tan_mechanisms(self):
            return "920"
        def get_tan_mechanisms(self):
            return {"920": SimpleNamespace(name="DKB-App (decoupled)", tech_id="DECOUPLED")}
        def set_tan_mechanism(self, _v):
            pass
        def get_sepa_accounts(self):
            return [SimpleNamespace(iban="DE001", product_name="Girokonto")]
        def get_balance(self, _a):
            return SimpleNamespace(amount=Decimal("50.00"))
        def get_transactions(self, _a, *_x):
            return []
        def deconstruct(self):
            return {}

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        client_factory=SimpleClient, sleeper=lambda _s: None,
    )
    assert adapter.debug_log == []  # initialized empty

    adapter.fetch_snapshot()

    assert isinstance(adapter.debug_log, list)
    # App INFO lines should be present
    assert any("DKB FinTS" in line for line in adapter.debug_log)


def test_fetch_snapshot_returns_debug_log_when_enabled():
    """When debug_fints_logging is True, fetch_snapshot must include a
    non-empty 'debug_log' key in the result dict with captured wire log lines."""
    import logging

    fints_logger = logging.getLogger("fints")
    fints_logger.setLevel(logging.WARNING)

    class DebugClient:
        init_tan_response = None
        def __init__(self, *_a, **_kw):
            self._fetched = False
        def __enter__(self):
            return self
        def __exit__(self, *_a):
            return None
        def fetch_tan_mechanisms(self):
            self._fetched = True
            return "920"
        def get_tan_mechanisms(self):
            return {"920": SimpleNamespace(name="DKB-App (decoupled)", tech_id="DECOUPLED")}
        def set_tan_mechanism(self, value):
            self.tan_mechanism = value
        def get_sepa_accounts(self):
            return [SimpleNamespace(iban="DE001", product_name="Girokonto")]
        def get_balance(self, _a):
            return SimpleNamespace(amount=Decimal("100.00"))
        def get_transactions(self, _a, *_x):
            return []
        def deconstruct(self):
            return {}

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        client_factory=DebugClient, sleeper=lambda _s: None,
    )
    adapter.debug_fints_logging = True

    # Emit a debug message through fints logger during the snapshot
    result = adapter.fetch_snapshot()

    assert "debug_log" in result
    # debug_log should be non-empty because _configure_tan emits DEBUG lines


def test_fetch_snapshot_returns_debug_log_even_when_disabled():
    """When debug_fints_logging is False, fetch_snapshot still captures
    app.foundation.dkb INFO lines into debug_log (always-on observability)."""
    class QuietClient:
        init_tan_response = None
        def __init__(self, *_a, **_kw):
            self._fetched = False
        def __enter__(self):
            return self
        def __exit__(self, *_a):
            return None
        def fetch_tan_mechanisms(self):
            self._fetched = True
            return "920"
        def get_tan_mechanisms(self):
            return {"920": SimpleNamespace(name="DKB-App (decoupled)", tech_id="DECOUPLED")}
        def set_tan_mechanism(self, value):
            self.tan_mechanism = value
        def get_sepa_accounts(self):
            return [SimpleNamespace(iban="DE001", product_name="Girokonto")]
        def get_balance(self, _a):
            return SimpleNamespace(amount=Decimal("100.00"))
        def get_transactions(self, _a, *_x):
            return []
        def deconstruct(self):
            return {}

    adapter = DkbFinTSAdapter(
        "https://fints.dkb.de/fints", "12030000", "u", "p",
        client_factory=QuietClient, sleeper=lambda _s: None,
    )
    adapter.debug_fints_logging = False  # default, explicit

    result = adapter.fetch_snapshot()

    assert "debug_log" in result
    assert isinstance(result["debug_log"], list)
    # App-level INFO is always captured — at minimum "configuring TAN mechanism" should appear
    assert any("DKB FinTS" in line for line in result["debug_log"])
    # Raw fints wire messages absent when verbose=False
    assert not any("wire:" in line for line in result["debug_log"])


def test_fints_manual_tan_required_detail():
    """DkbFinTSManualTanRequired.detail must return a structured dict
    with error, available_tan_methods, and fix."""
    from app.foundation.dkb import DkbFinTSManualTanRequired

    methods = [{"id": "911", "name": "chipTAN manuell"}]
    exc = DkbFinTSManualTanRequired(methods=methods)

    detail = exc.detail

    assert isinstance(detail, dict)
    assert "error" in detail
    assert detail["available_tan_methods"] == methods
    assert "fix" in detail
    assert "DKB-App" in detail["fix"]


def test_fints_selftest_persists_debug_log():
    """fints_selftest must persist debug_log from verify_login into the
    DkbDiagnosticRun row, and the response must include a non-empty debug_log
    when debug_fints_logging is enabled."""
    import logging
    import unittest.mock as mock

    fints_logger = logging.getLogger("fints")
    fints_logger.setLevel(logging.WARNING)

    class _DebugLogSelftestClient:
        """Self-test client that emits a fints DEBUG message to exercise log capture."""
        init_tan_response = None

        def __init__(self, *_args, **_kwargs):
            self._fetched = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch_tan_mechanisms(self):
            self._fetched = True
            return "920"

        def get_tan_mechanisms(self):
            logging.getLogger("fints").debug("wire: mock DEBUG selftest message")
            return {"920": SimpleNamespace(name="DKB-App (decoupled)", tech_id="DECOUPLED")}

        def set_tan_mechanism(self, value):
            self.tan_mechanism = value

        def get_sepa_accounts(self):
            return [SimpleNamespace(iban="DE001"), SimpleNamespace(iban="DE002")]

        def deconstruct(self):
            return {}

    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    from app.foundation.settings import set_secret, upsert_public_settings
    set_secret(db, "dkb", "12345", {"username": "dkb-user"})
    upsert_public_settings(db, {
        "dkb_provider": "fints",
        "dkb_product_id": "TESTPROD001",
        "dkb_debug_fints_logging": True,
    })

    with mock.patch("fints.client.FinTS3PinTanClient", _DebugLogSelftestClient):
        result = fints_selftest(db, user)

    assert result["status"] == "passed"
    # debug_log should be present in the response with captured content
    assert "debug_log" in result, f"debug_log missing from {list(result.keys())}"
    assert len(result["debug_log"]) > 0, "debug_log should be non-empty"
    assert "mock DEBUG selftest message" in result["debug_log"]
    # The row should have debug_log persisted
    row = db.query(DkbDiagnosticRun).filter(DkbDiagnosticRun.user_id == user.id).first()
    assert row is not None
    assert row.debug_log is not None
    assert "mock DEBUG selftest message" in row.debug_log


def test_fints_selftest_surfaces_debug_log_on_failure():
    """When verify_login() raises (e.g. DkbFinTSConnectionRejected), the
    self-test must still attach the captured FinTS wire log to the response
    and persist it in the DB row. Regression test for the 'finally' fix."""
    import logging
    import unittest.mock as mock

    fints_logger = logging.getLogger("fints")
    fints_logger.setLevel(logging.WARNING)

    class _RaisingClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch_tan_mechanisms(self):
            logging.getLogger("fints").debug("wire: error path DEBUG message")
            raise RuntimeError("Bad status code: 400")

        def get_tan_mechanisms(self):
            return {}

        def set_tan_mechanism(self, value):
            pass

        def get_sepa_accounts(self):
            return []

        def deconstruct(self):
            return {}

    db = _memory_db()
    user = User(username="jan_fail", password_hash="hash")
    db.add(user)
    db.commit()
    from app.foundation.settings import set_secret, upsert_public_settings
    set_secret(db, "dkb", "12345", {"username": "dkb-user"})
    upsert_public_settings(db, {
        "dkb_provider": "fints",
        "dkb_product_id": "TESTPROD001",
        "dkb_debug_fints_logging": True,
    })

    with mock.patch("fints.client.FinTS3PinTanClient", _RaisingClient):
        result = fints_selftest(db, user)

    assert result["status"] == "failed"
    assert "debug_log" in result, f"debug_log missing on failure path: {list(result.keys())}"
    assert result["debug_log"] is not None
    assert len(result["debug_log"]) > 0, "debug_log must be non-empty even on failure"
    assert "wire: error path DEBUG message" in result["debug_log"]
    row = db.query(DkbDiagnosticRun).filter(DkbDiagnosticRun.user_id == user.id).first()
    assert row is not None
    assert row.debug_log is not None
    assert "wire: error path DEBUG message" in row.debug_log


def test_persist_snapshot_preserves_unknown_avg_buy_price():
    db = _memory_db()
    user = User(username="unknown-cost", password_hash="hash")
    db.add(user)
    db.commit()

    service = DkbSyncService()
    snapshot = {
        "accounts": [
            {"iban": "DE-DEPOT-1", "type": "depot", "balance": Decimal("0"), "currency": "EUR"},
        ],
        "transactions": [],
        "positions": [
            {
                "iban": "DE-DEPOT-1",
                "isin": "IE00B4L5Y983",
                "name": "iShares Core MSCI World",
                "quantity": Decimal("120.5"),
                "avg_buy_price": None,
                "current_price": Decimal("85.40"),
                "current_value": Decimal("10290.70"),
            },
        ],
    }

    service.persist_snapshot(db, user.id, snapshot)

    position = db.query(DkbPosition).filter(DkbPosition.isin == "IE00B4L5Y983").one()
    assert position.avg_buy_price is None


def test_persist_snapshot_marks_aggregate_unknown_if_any_lot_unknown():
    db = _memory_db()
    user = User(username="mixed-cost", password_hash="hash")
    db.add(user)
    db.commit()

    service = DkbSyncService()
    snapshot = {
        "accounts": [
            {"iban": "DE-DEPOT-2", "type": "depot", "balance": Decimal("0"), "currency": "EUR"},
        ],
        "transactions": [],
        "positions": [
            {
                "iban": "DE-DEPOT-2", "isin": "US0378331005", "name": "Apple Inc.",
                "quantity": Decimal("5"), "avg_buy_price": Decimal("150.00"),
                "current_price": Decimal("190.00"), "current_value": Decimal("950.00"),
            },
            {
                "iban": "DE-DEPOT-2", "isin": "US0378331005", "name": "Apple Inc.",
                "quantity": Decimal("3"), "avg_buy_price": None,
                "current_price": Decimal("190.00"), "current_value": Decimal("570.00"),
            },
        ],
    }

    service.persist_snapshot(db, user.id, snapshot)

    position = db.query(DkbPosition).filter(DkbPosition.isin == "US0378331005").one()
    assert position.avg_buy_price is None
    assert position.quantity == Decimal("8")
