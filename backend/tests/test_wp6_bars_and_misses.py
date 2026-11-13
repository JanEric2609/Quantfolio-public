"""WP6 (audit 2026-11-12): partial bars and fetch-miss rules."""
import pandas as pd

from app.foundation.data_backbone.bar_guard import session_incomplete
from app.foundation.data_backbone.fetch_misses import RETRY_SOON, is_definitive_miss


def test_running_tokyo_session_is_incomplete_and_a_closed_one_is_not():
    day = pd.Timestamp("2026-11-12")  # a Thursday, XTKS session
    # 02:00 UTC = 11:00 JST: the session is running
    assert session_incomplete("7203.T", day, pd.Timestamp("2026-11-12 02:00", tz="UTC"))
    # 07:00 UTC = 16:00 JST: closed at 15:30 JST (06:30 UTC) + 30 min buffer
    assert not session_incomplete("7203.T", day, pd.Timestamp("2026-11-12 07:30", tz="UTC"))
    # yesterday's bar is final
    assert not session_incomplete("7203.T", pd.Timestamp("2026-11-11"), pd.Timestamp("2026-11-12 02:00", tz="UTC"))


def test_weekend_and_unknown_calendar_are_never_incomplete():
    assert not session_incomplete("AAPL", pd.Timestamp("2026-11-14"), pd.Timestamp("2026-11-14 12:00", tz="UTC"))
    assert not session_incomplete("USDEUR=X", pd.Timestamp("2026-11-12"), pd.Timestamp("2026-11-12 02:00", tz="UTC"))


def test_us_session_in_progress_is_incomplete():
    assert session_incomplete("AAPL", pd.Timestamp("2026-11-12"), pd.Timestamp("2026-11-12 16:00", tz="UTC"))


def test_throttle_timeout_and_short_history_are_not_misses():
    assert is_definitive_miss("No data returned")
    assert not is_definitive_miss("HTTP 429 Too Many Requests")
    assert not is_definitive_miss("yfinance: request timed out")
    assert not is_definitive_miss("provider throttled")
    assert not is_definitive_miss("history is short", short=True)
    assert not is_definitive_miss("only a session still open; retry after the close")


def test_wait_is_shorter_than_the_nightly_cadence():
    assert RETRY_SOON.total_seconds() == 20 * 3600
