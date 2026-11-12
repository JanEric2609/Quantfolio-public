from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_degraded_results_are_not_stored_and_real_rejections_are():
    from app.decision.discover import stage_cache as sc

    assert sc.is_degraded((None, "momentum_quality: no price data"))
    assert sc.is_degraded((None, "quant_signals: only 20 rows (< 60)"))
    assert sc.is_degraded(({"note": "benchmark or candidate price data unavailable"}, None))
    assert not sc.is_degraded((None, "verification_gate: volatility 120% a year exceeds 100%"))
    assert not sc.is_degraded(({"note": "", "concerns": []}, None))


def test_stage_computed_on_a_miss_sees_no_bar_after_the_key_date():
    from app.foundation import market
    from app.decision.discover import stage_cache as sc

    rows = [{"date": "2026-11-10", "close": 1.0}, {"date": "2026-11-11", "close": 2.0}, {"date": "2026-11-12", "close": 3.0}]
    seen = {}

    def fake_stage():
        seen["rows"] = market.history(None, "AAA")
        return {"ok": 1}, None

    db = _memory_db()
    with patch.object(market, "_history_rows", return_value=rows), patch.object(sc, "latest_bar_date", return_value="2026-11-11"):
        sc.cached_stage(db, "AAA", "momentum_quality", fake_stage)
    assert [r["date"] for r in seen["rows"]] == ["2026-11-10", "2026-11-11"]
    assert market.history_as_of.get() is None
