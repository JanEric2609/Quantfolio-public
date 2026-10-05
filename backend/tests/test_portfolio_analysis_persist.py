"""Tests for portfolio analysis persist layer (T3: ASCII crash + rollback safety)."""
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from conftest import _memory_db

from app.foundation.models.entities import StockResearchReport
from app.foundation.portfolio_analysis import generate_portfolio_report, save_portfolio_report
from app.foundation.text_safety import ascii_safe


def test_ascii_safe_round_trips_problematic_chars():
    """Verify that ascii_safe preserves UTF-8 Unicode and strips only control chars."""
    text = "a \u2014 b \u2013 c \u2192 d \u20ac e \u2018 f \u201c g"
    safe = ascii_safe(text)
    # Printable Unicode is preserved unchanged
    assert "\u2014" in safe   # em dash
    assert "\u2013" in safe   # en dash
    assert "\u2192" in safe   # rightwards arrow
    assert "\u20ac" in safe   # euro sign
    assert "\u2018" in safe   # left single curly quote
    assert "\u201c" in safe   # left double curly quote
    # Result round-trips through UTF-8 without error
    safe.encode("utf-8")


def test_save_portfolio_report_round_trips():
    """A report with problematic Unicode persists and reads back cleanly."""
    db = _memory_db()
    user_id = "test-user-1"
    report_text = "Performance \u2014 Q1 EUR 5\u20ac \u2192 \u201cup\u201d trend \u2014"
    summary = "\u2013 Summary: +5\u20ac \u2192 positive \u2014"

    obj = save_portfolio_report(db, user_id, report_text, summary, {"key": "val"})
    assert obj.id is not None
    assert obj.report_json is not None
    # Verify it round-trips through UTF-8 (Unicode is preserved, not ASCII-ified)
    obj.report_json.encode("utf-8")
    obj.executive_summary.encode("utf-8")

    # Read back from DB to confirm round-trip safety
    db.expire_all()
    reloaded = db.query(StockResearchReport).filter_by(id=obj.id).first()
    assert reloaded is not None
    reloaded.report_json.encode("utf-8")
    reloaded.executive_summary.encode("utf-8")


def test_save_portfolio_report_commit_failure_leaves_session_usable():
    """When commit fails after adding, the session is rolled back and remains usable."""
    db = _memory_db()
    user_id = "test-user-2"
    # Poison the session (add an uncommittable object with null FK constraint)
    orphan = StockResearchReport(
        ticker="ORPHAN",
        user_id=None,  # violates NOT NULL
        report_json="",
        executive_summary="",
        data_snapshot_json="{}",
        generated_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.add(orphan)

    # Report save should fail because orphan makes commit fail
    with pytest.raises(Exception):
        save_portfolio_report(db, user_id, "text", "summary", {})

    # Session is still usable after rollback
    assert db.query(StockResearchReport).count() >= 0
    # A clean query should work
    fresh = StockResearchReport(
        ticker="CLEAN",
        user_id=user_id,
        report_json="clean",
        executive_summary="clean",
        data_snapshot_json="{}",
        generated_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.add(fresh)
    db.commit()  # Should succeed


@patch("app.foundation.llm.router.call")
@patch("app.foundation.portfolio_analysis.build_portfolio_context")
def test_generate_portfolio_report_persist_failure_returns_persisted_false(
    mock_build_context, mock_llm_call
):
    """When save_portfolio_report fails, generate_portfolio_report returns persisted:False."""
    db = _memory_db()
    user_id = "test-user-3"

    mock_build_context.return_value = {
        "portfolio_summary": {
            "currency": "EUR",
            "total_value": 0,
            "cash_value": 0,
            "security_value": 0,
            "allocation_pct": {},
            "cashflow_30d": {},
        },
        "risk_profile": "moderate",
        "positions": [],
        "holdings": [],
        "stock_reports": {"reports": {}},
        "metrics": {},
        "portfolio": {"total_value": 0, "cash": 0},
        "regime": None,
        "snapshot": None,
    }

    mock_completion = MagicMock()
    mock_completion.content = "Test report \u2014 with Unicode \u20ac"
    mock_llm_call.return_value = mock_completion

    # Poison DB so save_portfolio_report commit fails
    orphan = StockResearchReport(
        ticker="ORPHAN",
        user_id=None,
        report_json="",
        executive_summary="",
        data_snapshot_json="{}",
        generated_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.add(orphan)

    import asyncio
    result = asyncio.run(generate_portfolio_report(db, user_id))

    assert result["persisted"] is False
    assert "report" in result  # Report text still returned
    # Session should not be poisoned
    db.rollback()
    count = db.query(StockResearchReport).count()
    assert count >= 0
