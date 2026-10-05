"""Bell-channel writer tests: notify.py must persist Notification rows."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _nudge(source="regime_change", severity="warning", payload='{"new_regime": "risk_off"}'):
    from app.foundation.models.entities import Nudge

    return Nudge(
        user_id="u1",
        source=source,
        severity=severity,
        payload_json=payload,
    )


def test_bell_channel_creates_notification_row():
    from app.foundation.models.entities import Notification, User
    from app.foundation.notify import send_notification

    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()

    nudge = _nudge()
    db.add(nudge)
    db.commit()

    result = send_notification(db, user.id, "bell", nudge)
    assert result is True

    notifications = db.query(Notification).all()
    assert len(notifications) == 1
    row = notifications[0]
    assert row.user_id == user.id
    assert row.source == "nudge"
    assert "regime" in row.title.lower()
    assert row.severity == "warning"
    assert row.read_at is None


def test_create_notification_helper_defaults():
    from app.foundation.models.entities import Notification, User
    from app.foundation.notify import create_notification

    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()

    row = create_notification(db, user.id, source="dkb", title="Sync failed", body="boom", severity="critical", href="/settings/dkb")
    assert isinstance(row, Notification)
    assert row.severity == "critical"
    assert row.href == "/settings/dkb"
    assert row.body == "boom"
    assert row.read_at is None

    fetched = db.query(Notification).filter(Notification.user_id == user.id).one()
    assert fetched.title == "Sync failed"
