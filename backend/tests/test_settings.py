import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import worker
from app.foundation.core.config import Settings
from app.foundation.core.db import Base
from app.foundation.core.security import SecretBox
from app.interface.api.settings import settings_schema
from app.interface.api.settings import test_integration as run_integration_test
from app.interface.api.tax import TaxSaleIn, record_sale, report_kap, seed_from_activity
from app.foundation.models.entities import ActivityLedgerEntry, LlmAuditEvent, QuantExperiment, TaxLedgerEvent, TaxLot, User
from app.foundation.llm.router import RouterConfig
from app.foundation.llm_audit import apply_prompt_retention_policy
from app.foundation.schemas import IntegrationTestRequest
from app.foundation.models.entities import ApiKey
from app.foundation.settings import (
    DEFAULT_PUBLIC_SETTINGS,
    SENSITIVE_INTEGRATIONS,
    get_connection_tests,
    get_secret,
    set_secret,
    update_secret_meta,
    upsert_public_settings,
)
from app.foundation.settings_catalog import CATALOG, CONNECTIONS, PAGE_GROUPS, validate_public_settings
from app.foundation.tax import german_tax_hints
from app.foundation.tax_cockpit import _estimation_allowed, compute_box3_year_overview, compute_year_overview, compute_year_overview_by_jurisdiction


def test_secret_box_local_fallback_is_stable_for_same_jwt_secret():
    settings = Settings(jwt_secret="stable-local-secret", encryption_key=None)
    first = SecretBox.from_settings(settings)
    second = SecretBox.from_settings(settings)

    encrypted = first.encrypt("dkb-pin")

    assert second.decrypt(encrypted) == "dkb-pin"


def test_secret_metadata_can_be_updated_without_replacing_secret():
    db = _memory_db()
    set_secret(db, "dkb", "bank-password", {"username": "old-user", "mfa_device": "m"})

    update_secret_meta(db, "dkb", {"username": "new-user"})

    secret, meta = get_secret(db, "dkb")
    assert secret == "bank-password"
    assert meta["username"] == "new-user"
    assert meta["mfa_device"] == "m"


def test_secret_update_preserves_existing_metadata_when_values_are_blank():
    db = _memory_db()
    set_secret(db, "dkb", "old-password", {"username": "dkb-user"})

    set_secret(db, "dkb", "new-password", {"username": ""})

    secret, meta = get_secret(db, "dkb")
    assert secret == "new-password"
    assert meta["username"] == "dkb-user"


# ---------------------------------------------------------------------------
# Prompt retention honours configured hours/days
# ---------------------------------------------------------------------------


def test_retention_honours_configured_hours():
    db = _memory_db()
    now = datetime.now(UTC)

    # Seed events: one recent (kept raw), one old enough to redact, one old enough to delete
    _seed_event(db, 0, now)                              # recent → stays raw
    _seed_event(db, 1, now - timedelta(hours=2))          # > 1h → should redact
    _seed_event(db, 2, now - timedelta(days=3))           # > 2d → should delete

    result = apply_prompt_retention_policy(db, now=now, raw_hours=1, delete_days=2)

    assert result["redacted"] == 1
    assert result["deleted"] == 1

    rows = db.query(LlmAuditEvent).order_by(LlmAuditEvent.prompt_json).all()
    # Row 0: still raw (prompt_json preserved)
    assert rows[0].retention_state == "raw"
    assert rows[0].prompt_json != "{}"
    # Row 1: redacted (prompt_json cleared, redacted set)
    assert rows[1].retention_state == "redacted"
    assert rows[1].prompt_json == "{}"
    assert rows[1].redacted_prompt_json is not None
    # Row 2: deleted
    assert rows[2].retention_state == "deleted"
    assert rows[2].prompt_json == "{}"
    assert rows[2].deleted_at is not None


def test_retention_noop_when_hours_and_days_are_large():
    db = _memory_db()
    now = datetime.now(UTC)

    _seed_event(db, 0, now - timedelta(hours=12))

    result = apply_prompt_retention_policy(db, now=now, raw_hours=24, delete_days=30)
    assert result["redacted"] == 0
    assert result["deleted"] == 0

    row = db.query(LlmAuditEvent).one()
    assert row.retention_state == "raw"


# ---------------------------------------------------------------------------
# Cloud fallback gated by llm_cloud_fallback_enabled
# ---------------------------------------------------------------------------


def test_cloud_fallback_gated_when_disabled(db: Session | None = None):
    _db = db or _memory_db()

    # Set cloud fallback disabled and insert cloud keys
    upsert_public_settings(_db, {"llm_cloud_fallback_enabled": False})
    set_secret(_db, "anthropic", "sk-ant-fake-key")
    set_secret(_db, "openai", "sk-proj-fake-key")

    config = RouterConfig(_db)
    backend, is_degraded = config.get_backend_for_task("batch_research")

    # Even with cloud keys present, should return local with degraded=True
    assert is_degraded is True
    assert "Local" in type(backend).__name__


def test_cloud_fallback_uses_cloud_when_enabled(db: Session | None = None):
    _db = db or _memory_db()

    upsert_public_settings(_db, {"llm_cloud_fallback_enabled": True})
    set_secret(_db, "anthropic", "sk-ant-fake-key")

    config = RouterConfig(_db)
    backend, is_degraded = config.get_backend_for_task("batch_research")

    assert is_degraded is False
    assert "Anthropic" in type(backend).__name__


def test_cloud_fallback_local_when_enabled_but_no_keys(db: Session | None = None):
    _db = db or _memory_db()

    upsert_public_settings(_db, {"llm_cloud_fallback_enabled": True})

    config = RouterConfig(_db)
    backend, is_degraded = config.get_backend_for_task("batch_research")

    assert is_degraded is True
    assert "Local" in type(backend).__name__


# ---------------------------------------------------------------------------
# Tax estimation gate
# ---------------------------------------------------------------------------


def test_tax_estimation_disabled_returns_disabled_payload():
    db = _memory_db()
    upsert_public_settings(db, {
        "tax_estimation_enabled": False,
        "tax_residency_country": "DE",
    })

    allowed, reason = _estimation_allowed(db)
    assert allowed is False
    assert reason is not None

    result = compute_year_overview(db, "no-user", 2024)
    assert result.get("disabled") is True
    assert result.get("estimate") is True
    assert result.get("not_tax_advice") is True


def test_tax_estimation_disabled_for_non_de_residency():
    db = _memory_db()
    upsert_public_settings(db, {
        "tax_estimation_enabled": True,
        "tax_residency_country": "NL",
    })

    allowed, reason = _estimation_allowed(db)
    assert allowed is False

    result = compute_year_overview(db, "no-user", 2024)
    assert result.get("disabled") is True


def test_tax_estimation_disabled_gates_box3():
    db = _memory_db()
    upsert_public_settings(db, {
        "tax_estimation_enabled": False,
        "tax_residency_country": "NL",
    })

    result = compute_box3_year_overview(
        db,
        "no-user",
        2024,
        holdings=[{"type": "equity", "amount_eur": 100_000}],
    )

    assert result.get("disabled") is True
    assert result.get("estimate") is True
    assert result.get("not_tax_advice") is True


def test_tax_box3_disabled_for_non_nl_residency():
    db = _memory_db()
    upsert_public_settings(db, {
        "tax_estimation_enabled": True,
        "tax_residency_country": "DE",
    })

    result = compute_box3_year_overview(
        db,
        "no-user",
        2024,
        holdings=[{"type": "equity", "amount_eur": 100_000}],
    )

    assert result.get("disabled") is True
    assert "box3_tax_eur" not in result.get("calculation", {})


def test_tax_box3_allowed_for_nl_residency():
    db = _memory_db()
    upsert_public_settings(db, {
        "tax_estimation_enabled": True,
        "tax_residency_country": "NL",
    })

    result = compute_box3_year_overview(
        db,
        "no-user",
        2024,
        holdings=[{"type": "equity", "amount_eur": 100_000}],
    )

    assert result.get("disabled") is None
    assert "box3_tax_eur" in result["calculation"]


def test_tax_overview_auto_non_de_non_nl_returns_disabled_payload():
    db = _memory_db()
    upsert_public_settings(db, {
        "tax_estimation_enabled": True,
        "tax_residency_country": "OTHER",
    })

    result = compute_year_overview_by_jurisdiction(db, "no-user", 2024)

    assert result.get("disabled") is True
    assert "not supported" in result.get("reason", "")


def test_tax_kap_report_returns_disabled_payload_without_shape_error():
    db = _memory_db()
    user = _user(db)
    upsert_public_settings(db, {
        "tax_estimation_enabled": False,
        "tax_residency_country": "DE",
    })

    result = report_kap(year=2024, db=db, user=user)

    assert result["form"] == "Anlage KAP"
    assert result.get("disabled") is True
    assert result.get("estimate") is True


def test_tax_hints_uncomputed_sections_flagged_not_implemented():
    # Vorabpauschale and dividend-tax sub-sections are not computed by the hints
    # endpoint; they must carry an explicit not_implemented marker rather than a
    # vague "placeholder" status that reads like fabricated data (issue #134).
    db = _memory_db()
    user = _user(db)
    upsert_public_settings(db, {
        "tax_estimation_enabled": True,
        "tax_residency_country": "DE",
    })

    result = german_tax_hints(db, user.id)

    assert result["vorabpauschale"]["not_implemented"] is True
    assert result["dividend_tax_estimate"]["not_implemented"] is True
    # The misleading "placeholder" status string is gone.
    assert result["dividend_tax_estimate"].get("status") != "placeholder"


def test_tax_hints_disabled_for_non_de_residency():
    db = _memory_db()
    user = _user(db)
    upsert_public_settings(db, {
        "tax_estimation_enabled": True,
        "tax_residency_country": "NL",
    })

    result = german_tax_hints(db, user.id)

    assert result.get("disabled") is True
    assert "capital_gains_tax_rate" not in result


def test_tax_fifo_disabled_does_not_persist_estimated_event():
    db = _memory_db()
    user = _user(db)
    _tax_lot(db, user.id)
    upsert_public_settings(db, {
        "tax_estimation_enabled": False,
        "tax_residency_country": "DE",
    })

    result = record_sale(
        TaxSaleIn(
            isin="DE0001234567",
            sell_date=date(2024, 6, 1),
            sell_quantity=1,
            sell_price_per_unit_eur=150,
        ),
        db=db,
        user=user,
    )

    assert result.get("disabled") is True
    assert db.query(TaxLedgerEvent).count() == 0


def test_tax_seed_from_activity_disabled_does_not_create_lots():
    db = _memory_db()
    user = _user(db)
    db.add(
        ActivityLedgerEntry(
            user_id=user.id,
            source="manual",
            dedupe_hash="settings-seed-disabled",
            activity_type="buy",
            date=date(2024, 1, 1),
            amount=Decimal("-1000"),
            description="Buy 10 shares",
            isin="DE0001234567",
            quantity=Decimal("10"),
            price=Decimal("100"),
            fees=Decimal("0"),
        )
    )
    db.commit()
    upsert_public_settings(db, {
        "tax_estimation_enabled": False,
        "tax_residency_country": "DE",
    })

    result = seed_from_activity(db=db, user=user)

    assert result.get("disabled") is True
    assert db.query(TaxLot).count() == 0


def test_tax_estimation_allowed_for_de():
    db = _memory_db()
    upsert_public_settings(db, {
        "tax_estimation_enabled": True,
        "tax_residency_country": "DE",
        "freistellungsauftrag_amount": 1000,
        "freistellungsauftrag_used": 0,
        "church_tax": "none",
        "tax_spouse_allowance": False,
    })

    allowed, reason = _estimation_allowed(db)
    assert allowed is True
    assert reason is None

    # Should succeed (no events → zero overview, not disabled)
    result = compute_year_overview(db, "no-user", 2024)
    assert result.get("disabled") is None or result.get("disabled") is False


# ---------------------------------------------------------------------------
# Worker jobs honour settings gates
# ---------------------------------------------------------------------------


def test_worker_recommendation_refresh_disabled_noops(monkeypatch):
    SessionLocal = _memory_session_factory()
    with SessionLocal() as db:
        _user(db, username="recommend-disabled")
        upsert_public_settings(db, {"scheduled_recommendation_refresh": False})

    calls: list[str] = []

    def fake_generate(db: Session, user_id: str, **kwargs):
        calls.append(user_id)
        return {"created": [{"id": "unexpected"}], "failed": []}

    monkeypatch.setattr(worker, "SessionLocal", SessionLocal)
    monkeypatch.setattr("app.decision.ai.generate_recommendations_for_user", fake_generate)

    assert worker.weekly_recommendation_refresh() == 0
    assert calls == []


def test_worker_recommendation_refresh_enabled_runs_for_users(monkeypatch):
    SessionLocal = _memory_session_factory()
    with SessionLocal() as db:
        user_a = _user(db, username="recommend-a")
        user_b = _user(db, username="recommend-b")
        upsert_public_settings(db, {"scheduled_recommendation_refresh": True})
        expected_ids = {user_a.id, user_b.id}

    calls: list[str] = []

    def fake_generate(db: Session, user_id: str, **kwargs):
        calls.append(user_id)
        return {"created": [{"id": f"rec-{user_id}"}], "failed": []}

    monkeypatch.setattr(worker, "SessionLocal", SessionLocal)
    monkeypatch.setattr("app.foundation.core.db.SessionLocal", SessionLocal)
    monkeypatch.setattr("app.decision.ai.generate_recommendations_for_user", fake_generate)

    assert worker.weekly_recommendation_refresh() == 2
    assert set(calls) == expected_ids


def test_worker_quant_refresh_disabled_noops(monkeypatch):
    SessionLocal = _memory_session_factory()
    with SessionLocal() as db:
        user = _user(db, username="quant-disabled")
        _experiment(db, user.id, active=True)
        upsert_public_settings(db, {"scheduled_quant_experiment_refresh": False})

    calls: list[str] = []

    def fake_run(db: Session, experiment: QuantExperiment):
        calls.append(experiment.id)

    monkeypatch.setattr(worker, "SessionLocal", SessionLocal)
    monkeypatch.setattr("app.foundation.quant_experiments.run_experiment_once", fake_run)

    assert worker.weekly_active_experiment_run() == 0
    assert calls == []


def test_worker_quant_refresh_enabled_runs_active_experiments(monkeypatch):
    SessionLocal = _memory_session_factory()
    with SessionLocal() as db:
        user = _user(db, username="quant-enabled")
        active = _experiment(db, user.id, active=True)
        _experiment(db, user.id, active=False)
        upsert_public_settings(db, {"scheduled_quant_experiment_refresh": True})
        active_id = active.id

    calls: list[str] = []

    def fake_run(db: Session, experiment: QuantExperiment):
        calls.append(experiment.id)

    monkeypatch.setattr(worker, "SessionLocal", SessionLocal)
    monkeypatch.setattr("app.foundation.core.db.SessionLocal", SessionLocal)
    monkeypatch.setattr("app.foundation.quant_experiments.run_experiment_once", fake_run)

    assert worker.weekly_active_experiment_run() == 1
    assert calls == [active_id]


def test_worker_cleanup_llm_audit_events_uses_configured_retention(monkeypatch):
    SessionLocal = _memory_session_factory()
    now = datetime.now(UTC)
    with SessionLocal() as db:
        _seed_event(db, 0, now)
        _seed_event(db, 1, now - timedelta(hours=2))
        _seed_event(db, 2, now - timedelta(days=3))
        upsert_public_settings(db, {
            "prompt_raw_retention_hours": 1,
            "prompt_delete_after_days": 2,
        })

    monkeypatch.setattr(worker, "SessionLocal", SessionLocal)
    monkeypatch.setattr("app.foundation.core.db.SessionLocal", SessionLocal)

    result = worker.cleanup_llm_audit_events()

    assert result["redacted"] == 1
    assert result["deleted"] == 1


# ---------------------------------------------------------------------------
# Settings schema completeness
# ---------------------------------------------------------------------------


def test_settings_schema_covers_every_public_setting_key():
    for key in DEFAULT_PUBLIC_SETTINGS:
        assert key in CATALOG, f"Missing catalog entry for public setting: {key}"


def test_settings_schema_covers_every_sensitive_integration():
    for key in SENSITIVE_INTEGRATIONS:
        assert key in CATALOG, f"Missing catalog entry for sensitive integration: {key}"


def test_every_sensitive_integration_belongs_to_a_connection():
    """The Control Center renders credentials only inside their connection drawer."""
    secrets = {c.secret for c in CONNECTIONS.values() if c.secret}
    assert secrets == SENSITIVE_INTEGRATIONS
    for connection in CONNECTIONS.values():
        if connection.secret:
            assert CATALOG[connection.secret].sensitive
            assert CATALOG[connection.secret].connection == connection.id


def test_connection_settings_live_on_their_connections_page():
    for key, entry in CATALOG.items():
        if entry.connection is not None:
            assert entry.connection in CONNECTIONS, key
            assert entry.group == CONNECTIONS[entry.connection].group, key


def test_optional_connection_settings_are_text_settings_of_that_connection():
    """``optional_settings`` exempts text settings from the "Not set up" check in the Control Center."""
    for connection in CONNECTIONS.values():
        for key in connection.optional_settings:
            assert CATALOG[key].connection == connection.id, key
            assert CATALOG[key].input_type == "text", key


def test_scalable_counts_as_set_up_with_its_defaults():
    """Scalable stores no credential, so only empty required settings mark it "Not set up".

    The portfolio ID stays empty on a one-portfolio account and the pin is
    optional, so every setting that defaults to empty must be optional.
    """
    scalable = CONNECTIONS["scalable"]
    empty = {
        key for key, entry in CATALOG.items()
        if entry.connection == "scalable" and entry.input_type in ("text", "number")
        and str(DEFAULT_PUBLIC_SETTINGS[key] or "").strip() == ""
    }
    assert empty <= set(scalable.optional_settings)
    assert "scalable_wrapper_path" not in scalable.optional_settings


def test_settings_schema_keys_are_stable():
    """Every catalog entry has required fields and sits on a Control Center page."""
    catalog = settings_schema(_user=object())["catalog"]
    for key, entry in CATALOG.items():
        d = catalog[key]
        assert d["group"] in PAGE_GROUPS, key
        assert d["label"] and d["help"], key
        assert d["input_type"] in ("text", "number", "boolean", "password", "select", "json", "date", "provider_chain")
        assert d["status"] in ("active", "needs_worker_restart", "experimental")
        if d["unit"]:
            assert d["input_type"] == "number", key
        if d["option_labels"]:
            assert set(d["option_labels"]) == set(d["options"]), key
        if not entry.sensitive:
            assert d["default"] == DEFAULT_PUBLIC_SETTINGS[key], key
        if entry.input_type == "json":
            assert isinstance(DEFAULT_PUBLIC_SETTINGS[key], dict), key


def test_catalog_defaults_are_valid_options():
    for key, entry in CATALOG.items():
        if entry.input_type == "select" and not entry.sensitive:
            assert DEFAULT_PUBLIC_SETTINGS[key] in entry.options, key


def test_validate_public_settings_rejects_url_display_name_and_non_object_json():
    errors = validate_public_settings({
        "webauthn_rp_name": "https://quantfolio.example",
        "isin_ticker_overrides": "[object Object]",
        "etf_index_map": None,
        "currency": "EUR",
    })

    assert set(errors) == {"webauthn_rp_name", "isin_ticker_overrides"}
    assert validate_public_settings({"webauthn_rp_name": "QuantFolio", "isin_ticker_overrides": {}}) == {}


def test_alpaca_secret_key_is_encrypted_not_stored_in_meta():
    db = _memory_db()
    set_secret(db, "alpaca", "PKTEST", {"api_secret": "very-secret"})

    row = db.query(ApiKey).filter(ApiKey.service == "alpaca").one()
    assert "very-secret" not in row.meta_json
    assert get_secret(db, "alpaca") == ("PKTEST", {"api_secret": "very-secret"})


def test_alpaca_key_replacement_keeps_the_stored_secret_key():
    db = _memory_db()
    set_secret(db, "alpaca", "PKOLD", {"api_secret": "kept"})

    set_secret(db, "alpaca", "PKNEW", {"api_secret": ""})

    assert get_secret(db, "alpaca") == ("PKNEW", {"api_secret": "kept"})


def test_alpaca_secret_key_alone_can_be_replaced():
    db = _memory_db()
    set_secret(db, "alpaca", "PKTEST", {"api_secret": "old"})

    update_secret_meta(db, "alpaca", {"api_secret": "new"})

    row = db.query(ApiKey).filter(ApiKey.service == "alpaca").one()
    assert "new" not in row.meta_json
    assert get_secret(db, "alpaca") == ("PKTEST", {"api_secret": "new"})


def test_legacy_plaintext_alpaca_secret_is_still_read_and_moved_on_save():
    db = _memory_db()
    box = SecretBox.from_settings()
    db.add(ApiKey(service="alpaca", key_encrypted=box.encrypt("PKLEGACY"), meta_json=json.dumps({"api_secret": "plain"})))
    db.commit()

    assert get_secret(db, "alpaca") == ("PKLEGACY", {"api_secret": "plain"})

    set_secret(db, "alpaca", "PKLEGACY")

    row = db.query(ApiKey).filter(ApiKey.service == "alpaca").one()
    assert "plain" not in row.meta_json
    assert get_secret(db, "alpaca") == ("PKLEGACY", {"api_secret": "plain"})


def test_integration_test_of_saved_config_is_recorded():
    db = _memory_db()

    response = run_integration_test(IntegrationTestRequest(service="yfinance"), db=db, user=object())
    unsaved = run_integration_test(IntegrationTestRequest(service="finnhub", value="typed-not-saved"), db=db, user=object())

    assert response.tested_at is not None
    assert unsaved.tested_at is None
    tests = get_connection_tests(db)
    assert set(tests) == {"yfinance"}
    assert tests["yfinance"]["ok"] == response.ok


def test_settings_schema_endpoint_returns_catalog_and_completeness():
    payload = settings_schema(_user=object())

    assert "catalog" in payload
    assert payload["catalog"]["tax_residency_country"]["options"] == ["DE", "NL", "OTHER"]
    assert set(payload["completeness"]["public_setting_keys"]) == set(DEFAULT_PUBLIC_SETTINGS)
    assert set(payload["completeness"]["sensitive_integration_keys"]) == SENSITIVE_INTEGRATIONS


# ---------------------------------------------------------------------------
# Test-connection dispatch: every sensitive integration must be a valid
# IntegrationTestRequest.service member, or the endpoint 422s before the
# real per-provider check ever runs (surfaced to the UI as a misleading
# "Connection test failed: Validation failed" for a provider that isn't
# actually broken).
# ---------------------------------------------------------------------------
def test_integration_test_request_accepts_every_sensitive_integration():
    from app.foundation.schemas import IntegrationTestRequest

    for key in SENSITIVE_INTEGRATIONS:
        IntegrationTestRequest(service=key)  # raises ValidationError if not a Literal member


def test_integration_test_dispatches_no_key_status_providers():
    from app.interface.api.settings import test_integration
    from app.foundation.schemas import IntegrationTestRequest

    db = _memory_db()
    for service in ("ecb_sdw", "yfinance", "justetf"):
        response = test_integration(IntegrationTestRequest(service=service), db=db, user=object())
        assert response.service == service
        assert response.message != "Validation failed"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_event(db: Session, idx: int, created_at: datetime) -> LlmAuditEvent:
    row = LlmAuditEvent(
        id=f"evt-{idx}",
        user_id="test-user",
        agent="test",
        purpose="test",
        prompt_json=json.dumps({"prompt": f"test-{idx}"}),
        response_json=json.dumps({"response": f"test-{idx}"}),
        retention_state="raw",
        pinned=False,
        created_at=created_at,
    )
    db.add(row)
    db.commit()
    return row


def _user(db: Session, username: str = "jan") -> User:
    user = User(username=username, password_hash="hash")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _experiment(db: Session, user_id: str, *, active: bool) -> QuantExperiment:
    experiment = QuantExperiment(
        user_id=user_id,
        name=f"Experiment {active}",
        description=None,
        mode="long_term",
        universe_json=json.dumps(["EUNL.DE"]),
        benchmark_symbol="EUNL.DE",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        rebalance_frequency="monthly",
        strategy_type="buy_hold",
        config_json="{}",
        active=active,
    )
    db.add(experiment)
    db.commit()
    db.refresh(experiment)
    return experiment


def _tax_lot(db: Session, user_id: str) -> TaxLot:
    lot = TaxLot(
        user_id=user_id,
        isin="DE0001234567",
        symbol="TEST",
        name="Test Holding",
        fund_class="aktien",
        teilfreistellung_pct=Decimal("0.30"),
        acquired_at=date(2024, 1, 1),
        quantity_initial=Decimal("10"),
        quantity_remaining=Decimal("10"),
        cost_basis_eur=Decimal("1000"),
        fees_eur=Decimal("0"),
        source="manual",
    )
    db.add(lot)
    db.commit()
    db.refresh(lot)
    return lot


def _memory_session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _memory_db():
    return _memory_session_factory()()


# ---------------------------------------------------------------------------
# PUT /api/settings: plan keys persist, null resets, secrets never go public
# ---------------------------------------------------------------------------

def test_update_settings_persists_plan_keys_and_null_resets_to_default():
    from app.foundation.schemas.common import SettingsPayload
    from app.foundation.settings import get_public_settings
    from app.interface.api.settings import update_settings

    db = _memory_db()
    update_settings(SettingsPayload(settings={"monthly_contribution_eur": 500, "plan_tilt_isins": "IE00BP3QZ825"}), db, None)
    stored = get_public_settings(db)
    assert stored["monthly_contribution_eur"] == 500
    assert stored["plan_tilt_isins"] == "IE00BP3QZ825"

    update_settings(SettingsPayload(settings={"plan_tilt_isins": None}), db, None)
    stored = get_public_settings(db)
    assert stored["plan_tilt_isins"] == DEFAULT_PUBLIC_SETTINGS["plan_tilt_isins"]
    assert stored["monthly_contribution_eur"] == 500


def test_update_settings_never_stores_a_secret_integration_as_plaintext():
    from app.foundation.models.entities import AppSetting
    from app.foundation.schemas.common import SettingsPayload
    from app.interface.api.settings import update_settings

    db = _memory_db()
    update_settings(SettingsPayload(settings={"llm": "sk-local", "finnhub": "fh-key", "currency": "EUR"}), db, None)
    keys = {row.key for row in db.query(AppSetting).all()}
    assert "llm" not in keys and "finnhub" not in keys
    assert "currency" in keys


def test_every_secret_integration_catalog_entry_is_marked_sensitive():
    # The settings page renders a non-sensitive entry as a plain text field
    # and saves it as a public setting; a credential must never take that path.
    for key in SENSITIVE_INTEGRATIONS:
        assert CATALOG[key].sensitive, f"{key} is a secret integration but its catalog entry is not sensitive"
