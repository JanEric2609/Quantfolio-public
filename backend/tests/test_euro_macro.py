"""Euro-area macro series and the macro fact block (plan 2026-10-12, D11 / WP8)."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from conftest import _memory_db

from app.foundation import market
from app.foundation.euro_macro import macro_fact_block, refresh_ecb_series, store_series
from app.foundation.models.entities import MacroIndicator
from app.foundation.providers.ecb_provider import EcbProvider

FIXTURE = Path(__file__).parent / "fixtures" / "ecb_dfr_2026-09.json"


def test_no_fallback_row_is_ever_inserted():
    db = _memory_db()
    assert market.macro_indicators(db) == []
    assert db.query(MacroIndicator).count() == 0


def test_dfr_parses_from_a_recorded_ecb_response():
    resp = MagicMock()
    resp.json.return_value = json.loads(FIXTURE.read_text())
    resp.raise_for_status.return_value = None
    client = MagicMock()
    client.__enter__.return_value.get.return_value = resp
    with patch("app.foundation.providers.ecb_provider.httpx.Client", return_value=client):
        result = EcbProvider().get_history("FM.D.U2.EUR.4F.KR.DFR.LEV", start="2026-09-14", end="2026-09-18")
    assert result["ok"]
    rows = [(o["date"], o["value"]) for o in result["data"]]
    # The 2026-09-16 hike from 2.25 % to 2.50 %.
    assert rows[0] == ("2026-09-14", 2.25)
    assert ("2026-09-16", 2.5) in rows
    assert [d for d, _ in rows] == sorted(d for d, _ in rows)


def _daily(start: date, end: date, steps: dict[date, float], first: float) -> list[dict]:
    out, level, d = [], first, start
    while d <= end:
        level = steps.get(d, level)
        out.append({"date": d.isoformat(), "value": level})
        d += timedelta(days=1)
    return out


def test_fact_block_reports_level_last_change_and_12m_direction():
    db = _memory_db()
    today = date(2026, 10, 12)
    store_series(db, "ECB_DFR", _daily(date(2025, 1, 1), today, {
        date(2025, 6, 11): 2.0, date(2026, 6, 17): 2.25, date(2026, 9, 16): 2.5,
    }, 2.75))
    store_series(db, "DFEDTARU", _daily(date(2025, 1, 1), today, {date(2025, 12, 11): 3.75}, 4.5), source="fred")
    store_series(db, "HICP_EA", [{"date": "2026-09-01", "value": 3.8}])
    db.commit()

    block = macro_fact_block(db, today=today)

    ecb = block["ecb_deposit_rate"]
    assert ecb["level_pct"] == 2.5
    assert ecb["last_change"] == {"date": "2026-09-16", "from": 2.25, "to": 2.5}
    assert ecb["direction_12m"] == "hikes"
    assert block["rate_direction_12m"] == {"ecb": "hikes", "fed": "cuts"}
    assert block["hicp_euro_area"] == {"value_pct": 3.8, "month": "2026-09"}
    assert block["hicp_euro_area_core"] is None


def test_without_data_the_direction_is_unknown():
    block = macro_fact_block(_memory_db(), today=date(2026, 10, 12))
    assert block["rate_direction_12m"] == {"ecb": None, "fed": None}


def test_refresh_stores_each_series_under_source_ecb():
    db = _memory_db()
    provider = MagicMock()
    provider.get_history.return_value = {"ok": True, "data": [{"date": "2026-09-01", "value": 1.5}]}
    out = refresh_ecb_series(db, provider)
    assert all(v["ok"] for v in out.values())
    names = {r.name for r in db.query(MacroIndicator).filter(MacroIndicator.source == "ecb")}
    assert names == {"ECB_DFR", "ECB_MRO", "HICP_EA", "HICP_EA_CORE"}
    asked = [c.args[0] for c in provider.get_history.call_args_list]
    assert "HICP.M.U2.N.000000.4D0.ANR" in asked  # the post-2026 HICP dataset, not the frozen ICP one


def test_latest_row_per_series_not_25_rows_of_estr():
    db = _memory_db()
    store_series(db, "ESTR", [{"date": (date(2026, 9, 1) + timedelta(days=i)).isoformat(), "value": 2.4}
                              for i in range(40)])
    store_series(db, "ECB_DFR", [{"date": "2026-09-16", "value": 2.5}])
    db.commit()
    names = [r["name"] for r in market.macro_indicators(db)]
    assert sorted(names) == ["ECB_DFR", "ESTR"]
