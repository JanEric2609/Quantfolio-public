"""Upcoming earnings reports in Discover dossiers (2026-09-28 audit: MU
reported two days after a run whose dossier never mentioned it)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

from conftest import _memory_db

from app.decision.discover.dossier_writer import _earnings_text
from app.decision.discover.pipeline import attach_earnings_calendar
from app.foundation.providers.base import provider_result
from app.foundation.providers.finnhub_provider import FinnhubProvider
from app.foundation.providers.yfinance_provider import YFinanceProvider

TODAY = datetime.now(UTC).date()


class _Ticker:
    def __init__(self, calendar):
        self.calendar = calendar


def test_yfinance_returns_the_next_upcoming_report_and_an_unconfirmed_window():
    window = [TODAY + timedelta(days=5), TODAY + timedelta(days=9)]
    with patch("yfinance.Ticker", return_value=_Ticker({"Earnings Date": window})):
        result = YFinanceProvider().get_earnings_calendar("SAP.DE")

    assert result["ok"] is True
    assert result["data"]["next_earnings_date"] == window[0].isoformat()
    assert result["data"]["earnings_date_to"] == window[1].isoformat()


def test_yfinance_ignores_past_reports():
    with patch("yfinance.Ticker", return_value=_Ticker({"Earnings Date": [TODAY - timedelta(days=3)]})):
        result = YFinanceProvider().get_earnings_calendar("SAP.DE")

    assert result["ok"] is False


def test_finnhub_declines_non_us_listings_without_a_request():
    provider = FinnhubProvider(api_key="k")
    with patch.object(FinnhubProvider, "_get", side_effect=AssertionError("no request expected")):
        result = provider.get_earnings_calendar("ASML.AS")

    assert result["ok"] is False


def test_finnhub_picks_the_earliest_upcoming_us_report():
    provider = FinnhubProvider(api_key="k")
    later, sooner = (TODAY + timedelta(days=78)).isoformat(), (TODAY + timedelta(days=2)).isoformat()
    payload = {"earningsCalendar": [
        {"symbol": "MU", "date": later, "hour": "amc"},
        {"symbol": "MU", "date": sooner, "hour": "amc"},
    ]}
    with patch.object(FinnhubProvider, "_get", return_value=payload):
        result = provider.get_earnings_calendar("MU")

    assert result["data"] == {"symbol": "MU", "next_earnings_date": sooner, "earnings_date_to": None, "hour": "amc"}


def _registry_returning(data):
    class _Registry:
        def get_earnings_calendar(self, symbol):
            return provider_result("yfinance", ok=data is not None, data=data)

    return _Registry()


def test_a_report_within_two_weeks_becomes_the_first_concern():
    db = _memory_db()
    report = (TODAY + timedelta(days=2)).isoformat()
    scores = {"sentiment_fundamentals": {"pe": 24.2}, "concerns": ["analyst_unavailable"]}
    data = {"next_earnings_date": report, "earnings_date_to": None, "hour": "amc"}
    with patch("app.decision.discover.pipeline.build_provider_registry", return_value=_registry_returning(data)):
        out = attach_earnings_calendar(db, "MU", scores)

    assert out["concerns"][0] == f"earnings on {report} (in 2 days)"
    assert out["sentiment_fundamentals"]["pe"] == 24.2
    assert out["sentiment_fundamentals"]["days_to_earnings"] == 2
    assert out["sentiment_fundamentals"]["earnings_hour"] == "amc"


def test_a_distant_report_is_recorded_without_a_concern():
    db = _memory_db()
    data = {"next_earnings_date": (TODAY + timedelta(days=40)).isoformat(), "earnings_date_to": None, "hour": None}
    with patch("app.decision.discover.pipeline.build_provider_registry", return_value=_registry_returning(data)):
        out = attach_earnings_calendar(db, "SIE.DE", {"concerns": []})

    assert out["concerns"] == []
    assert out["sentiment_fundamentals"]["days_to_earnings"] == 40


def test_no_calendar_leaves_scores_unchanged():
    db = _memory_db()
    scores = {"sentiment_fundamentals": {"pe": 10.0}, "concerns": []}
    with patch("app.decision.discover.pipeline.build_provider_registry", return_value=_registry_returning(None)):
        out = attach_earnings_calendar(db, "XYZ", dict(scores))

    assert out == scores


def test_dossier_text_names_the_date_and_session():
    sf = {"next_earnings_date": "2026-09-30", "days_to_earnings": 2, "earnings_hour": "amc", "earnings_date_to": None}
    assert _earnings_text(sf) == "Next earnings report: 2026-09-30, after the close, 2 days from now."
    unconfirmed = {**sf, "earnings_hour": None, "earnings_date_to": "2026-10-02"}
    assert _earnings_text(unconfirmed) == (
        "Next earnings report: 2026-09-30 to 2026-10-02 (not confirmed), 2 days from now."
    )
    assert _earnings_text({}) is None
    assert date.fromisoformat(sf["next_earnings_date"])  # stays an ISO date
