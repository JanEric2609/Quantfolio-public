"""WP1 (plan 2026-11-10): Alpaca history asks for a start date and follows pages."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.foundation.providers.alpaca_provider import AlpacaProvider


def _provider(monkeypatch, pages):
    p = AlpacaProvider.__new__(AlpacaProvider)
    p.enabled = True
    p.api_key = "k"
    p.api_secret = "s"
    calls: list[dict] = []

    def _get(path, params=None):
        calls.append(dict(params or {}))
        return pages[len(calls) - 1]

    monkeypatch.setattr(p, "_get", _get)
    return p, calls


def _bar(day: str) -> dict:
    return {"t": f"{day}T05:00:00Z", "o": 1, "h": 2, "l": 1, "c": 1.5, "v": 10}


def test_days_without_start_sends_a_start_and_follows_pages(monkeypatch):
    pages = [
        {"bars": [_bar("2026-11-05"), _bar("2026-11-06")], "next_page_token": "t1"},
        {"bars": [_bar("2026-11-09")], "next_page_token": None},
    ]
    p, calls = _provider(monkeypatch, pages)
    out = p.get_history("AAPL", days=1260)
    assert out["ok"]
    assert len(out["data"]) == 3
    assert len(calls) == 2
    assert calls[0]["limit"] == 10000
    start = datetime.fromisoformat(calls[0]["start"]).date()
    assert (datetime.now(UTC).date() - start) >= timedelta(days=1260 * 1.6 - 1)
    assert calls[1]["page_token"] == "t1"


def test_explicit_start_is_kept(monkeypatch):
    p, calls = _provider(monkeypatch, [{"bars": [_bar("2026-11-09")]}])
    p.get_history("AAPL", start="2026-11-01")
    assert calls[0]["start"] == "2026-11-01"
