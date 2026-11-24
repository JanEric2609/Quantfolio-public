from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.decision.desk import cfd_instruments as ci
from app.decision.desk.signals import owner_calls as oc

NOW = datetime(2026, 11, 24, 16, 0, tzinfo=UTC)


def test_parse_forms():
    i = oc.parse_idea("/idea NVDA down cfd")
    assert i.direction == "short" and i.product_types == ("cfd",) and i.cfd_stop is None and i.hold_days is None
    i = oc.parse_idea("/idea SAP.DE up cfd stop 170.5 10d")
    assert i.direction == "long" and i.cfd_stop == "170.5" and i.hold_days == 10
    i = oc.parse_idea("/idea NVDA up cfd stop 3% today")
    assert i.cfd_stop == "3%" and i.hold_days == 0
    assert oc.parse_idea("/idea NVDA up cfd 1d").hold_days == 1
    assert oc.parse_idea("/idea NVDA up cfd stop 2,5%").cfd_stop == "2.5%"


def test_parse_errors_and_legacy_unchanged():
    for bad in ("/idea NVDA up cfd stop", "/idea NVDA up cfd stop abc", "/idea NVDA up cfd 11d",
                "/idea NVDA up cfd stop 0%", "/idea NVDA up cfd banana"):
        with pytest.raises(oc.IdeaParseError):
            oc.parse_idea(bad)
    legacy = oc.parse_idea("/idea NVDA up 1d ko")
    assert legacy.product_types == ("knockout",) and legacy.cfd_stop is None
    with pytest.raises(oc.IdeaParseError):
        oc.parse_idea("/idea NVDA up 5d")  # still longer than the desk holds for non-CFD


def test_candidate_defaults_and_stops(monkeypatch):
    monkeypatch.setattr(ci, "reference_price", lambda *a, **k: Decimal("100"))
    monkeypatch.setattr(ci, "atr14", lambda *a, **k: Decimal("2"))
    c = oc.cfd_candidate(None, oc.parse_idea("/idea NVDA down cfd"), None, NOW)
    assert c.cell == "owner_call:cfd" and c.stop_distance == Decimal("3.0") and c.direction == "short"
    assert c.evidence["hold_days"] == 5 and c.max_exit_at > NOW
    c = oc.cfd_candidate(None, oc.parse_idea("/idea NVDA up cfd stop 4%"), None, NOW)
    assert c.stop_distance == Decimal("4")
    c = oc.cfd_candidate(None, oc.parse_idea("/idea NVDA up cfd stop 95"), None, NOW)
    assert c.stop_distance == Decimal("5")
    assert "wrong side" in oc.cfd_candidate(None, oc.parse_idea("/idea NVDA up cfd stop 105"), None, NOW)
    assert "not a ticker" in oc.cfd_candidate(None, oc.parse_idea("/idea ??? up cfd"), None, NOW)
    monkeypatch.setattr(ci, "reference_price", lambda *a, **k: None)
    assert "No fresh price" in oc.cfd_candidate(None, oc.parse_idea("/idea NVDA up cfd"), None, NOW)
