from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.foundation.core.db import Base
from app.foundation.models.entities import LlmAuditEvent, StrategyGraveyardEntry, User
from app.foundation.llm_audit import apply_prompt_retention_policy
from app.foundation.providers.openbb_provider import OpenBBProvider
from app.foundation.providers.registry import ProviderRegistry
from app.decision.ai import _default_universe as default_universe


def test_openbb_unavailable_returns_warning_not_crash():
    provider = OpenBBProvider(enabled=False)

    result = provider.get_quote("IWDA.AS")

    assert result["ok"] is False
    assert result["quality"]["warnings"]


def test_provider_registry_falls_back_to_second_provider():
    class EmptyProvider:
        enabled = True
        name = "empty"
        capabilities = {"get_quote"}

        def status(self):
            return {"provider": self.name, "enabled": True, "available": True, "message": "empty"}

        def get_quote(self, symbol):
            return {"ok": False, "provider": self.name, "quality": {"warnings": ["empty"]}, "error": "empty"}

    class GoodProvider(EmptyProvider):
        name = "good"

        def get_quote(self, symbol):
            return {"ok": True, "provider": self.name, "data": {"symbol": symbol}, "quality": {"warnings": []}, "error": None}

    registry = ProviderRegistry([EmptyProvider(), GoodProvider()])

    assert registry.get_quote("SAP.DE")["provider"] == "good"


def test_no_crypto_in_default_universes():
    for universe in ("default_global", "etfs", "stocks"):
        assert all(item.get("type") != "crypto" for item in default_universe(universe))


def test_prompt_retention_redacts_then_deletes():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        user = User(username="jan", password_hash="hash")
        db.add(user)
        db.commit()
        raw = LlmAuditEvent(
            user_id=user.id,
            agent="test",
            purpose="unit",
            prompt_json='{"holding":"IE00B4L5Y983","value":12345.67}',
            response_json='{"answer":"value 12345"}',
            created_at=datetime.now(UTC) - timedelta(hours=25),
        )
        old = LlmAuditEvent(
            user_id=user.id,
            agent="test",
            purpose="unit",
            prompt_json='{"holding":"IE00B4L5Y983"}',
            response_json='{}',
            created_at=datetime.now(UTC) - timedelta(days=15),
        )
        db.add_all([raw, old])
        db.commit()

        result = apply_prompt_retention_policy(db)

        assert result == {"redacted": 1, "deleted": 1}
        assert raw.retention_state == "redacted"
        assert old.retention_state == "deleted"


def test_strategy_graveyard_model_is_additive():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        user = User(username="jan", password_hash="hash")
        db.add(user)
        db.commit()
        entry = StrategyGraveyardEntry(user_id=user.id, name="Failed momentum", reason="No trades")
        db.add(entry)
        db.commit()
        assert db.query(StrategyGraveyardEntry).count() == 1
