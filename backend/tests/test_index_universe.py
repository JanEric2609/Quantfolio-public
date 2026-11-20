"""MSCI World constituents as a universe source, the analyst-target cache and the nightly prefetch."""
from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.discover import universe
from app.decision.discover.jobs import discover_universe_prefetch_inner
from app.foundation import analyst_cache, index_universe
from app.foundation.core.db import Base
from app.foundation.models.entities import AnalystConsensusCache, Fundamental, IndexUniverseMember


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _xlsx(rows: list[list[str]]) -> bytes:
    def cell(ref: str, value: str) -> str:
        return f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>'

    body = "".join(
        f'<row r="{i + 1}">' + "".join(cell(f"{chr(65 + j)}{i + 1}", v) for j, v in enumerate(row)) + "</row>"
        for i, row in enumerate(rows)
    )
    sheet = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             f"<sheetData>{body}</sheetData></worksheet>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as book:
        book.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


HEADER = ["ISIN", "SEDOL", "Security Name", "Currency", "Number of Shares", "Percent of Fund",
          "Trade Country Name", "Local Price", "Sector Classification", "Industry Classification"]


def _file(rows):
    return _xlsx([
        ["Fund Name:", "SPDR MSCI World"], ["Holdings As Of:", "05-Nov-2026"], [], HEADER,
        *rows,
        ["", "", "Cash", "USD", "", "0.1", "", "", "", ""],
    ])


APPLE = ["US0378331005", "2046251", "Apple Inc.", "USD", "1", "5.7", "United States", "344", "Information Technology", "Hardware"]
VOLVO = ["SE0000115446", "x", "Volvo AB Class B", "SEK", "1", "0.1", "Sweden", "300", "Industrials", "Machinery"]
AIA = ["HK0000069689", "x", "AIA Group Limited", "HKD", "1", "0.2", "Hong Kong", "80", "Financials", "Insurance"]
SAP = ["DE0007164600", "x", "SAP SE", "EUR", "1", "0.4", "Germany", "200", "Information Technology", "Software"]


def test_parse_keeps_equities_and_their_classification():
    holdings, as_of = index_universe.parse_holdings(_file([APPLE, VOLVO]))
    assert as_of == date(2026, 11, 5)
    assert [h.isin for h in holdings] == ["US0378331005", "SE0000115446"]
    assert holdings[0].sector == "Information Technology" and holdings[0].currency == "USD"
    assert holdings[1].country == "Sweden" and holdings[1].weight == 0.1


def test_yahoo_symbols_per_exchange():
    assert index_universe.yahoo_symbol("BRK/B", "US") == "BRK-B"
    assert index_universe.yahoo_symbol("BP/", "LN") == "BP.L"
    assert index_universe.yahoo_symbol("BT/A", "LN") == "BT-A.L"
    assert index_universe.yahoo_symbol("5", "HK") == "0005.HK"
    assert index_universe.yahoo_symbol("7203", "JT") == "7203.T"
    assert index_universe.yahoo_symbol("VOLVB", "SS", "Volvo AB Class B") == "VOLV-B.ST"
    assert index_universe.yahoo_symbol("NOVOB", "DC", "Novo Nordisk A/S Class B") == "NOVO-B.CO"
    assert index_universe.yahoo_symbol("ABB", "SS", "ABB Ltd") == "ABB.ST"
    assert index_universe.yahoo_symbol("BBD/B", "CN") == "BBD-B.TO"
    assert index_universe.yahoo_symbol("X", "ZZ") is None


class FakeFigi:
    """Answers each job with its listing on the requested exchange, if it has one."""

    def __init__(self, listings: dict[tuple[str, str], str]):
        self.listings = listings
        self.calls: list[list[dict]] = []

    def __call__(self, jobs):
        self.calls.append(jobs)
        out = []
        for job in jobs:
            ticker = self.listings.get((job["idValue"], job["exchCode"]))
            out.append({"data": [{"ticker": ticker, "exchCode": job["exchCode"], "marketSector": "Equity"}]}
                       if ticker else {"warning": "No identifier found."})
        return out


def test_finnish_class_shares_keep_their_ticker():
    # Elisa Oyj Class A is ELISA.HE on Yahoo (trial of 2026-11-08).
    assert index_universe.yahoo_symbol("ELISA", "FH", "Elisa Oyj Class A") == "ELISA.HE"


def test_a_rate_limited_batch_is_sent_again():
    import requests

    holdings, _ = index_universe.parse_holdings(_file([APPLE]))
    calls = []

    def post(jobs):
        calls.append(jobs)
        if len(calls) == 1:
            response = requests.Response()
            response.status_code = 429
            raise requests.HTTPError("429 Too Many Requests", response=response)
        return [{"data": [{"ticker": "AAPL", "exchCode": "US", "marketSector": "Equity"}]}]

    assert index_universe.map_symbols(holdings, post=post, pause=0) == {"US0378331005": "AAPL"}
    assert len(calls) == 2


def test_map_symbols_asks_the_home_exchange_and_falls_back_in_order():
    holdings, _ = index_universe.parse_holdings(_file([APPLE, VOLVO, AIA]))
    figi = FakeFigi({("US0378331005", "US"): "AAPL", ("SE0000115446", "SS"): "VOLVB", ("HK0000069689", "HK"): "1299"})
    assert index_universe.map_symbols(holdings, post=figi, pause=0) == {
        "US0378331005": "AAPL", "SE0000115446": "VOLV-B.ST", "HK0000069689": "1299.HK",
    }
    # Germany has a second code to try; Japan too. One batch here, all found on the first code.
    assert len(figi.calls) == 1 and figi.calls[0][0] == {"idType": "ID_ISIN", "idValue": "US0378331005", "exchCode": "US"}


def test_refresh_uses_known_tickers_maps_the_rest_and_drops_leavers():
    db = _memory_db()
    db.add(Fundamental(ticker="SAP.DE", data_json=json.dumps({"isin": "DE0007164600"}), source="yfinance"))
    db.add(IndexUniverseMember(index_key="msci_world", isin="XX0000000001", name="Gone plc", symbol="GONE.L",
                               updated_at=datetime.now(UTC)))
    db.commit()
    figi = FakeFigi({("US0378331005", "US"): "AAPL"})
    report = index_universe.refresh_members(db, fetch=lambda code: _file([APPLE, SAP, AIA]), post=figi, pause=0)
    db.commit()
    rows = {m.isin: m for m in db.query(IndexUniverseMember).all()}
    assert set(rows) == {"US0378331005", "DE0007164600", "HK0000069689"}
    assert (rows["DE0007164600"].symbol, rows["DE0007164600"].symbol_source) == ("SAP.DE", "known")
    assert (rows["US0378331005"].symbol, rows["US0378331005"].symbol_source) == ("AAPL", "openfigi")
    assert rows["HK0000069689"].symbol is None
    assert report == {"as_of": "2026-11-05", "members": 3, "removed": 1, "mapped_known": 1,
                      "mapped_openfigi": 1, "unmapped": 1}
    # SAP was known, so OpenFIGI was never asked for it.
    assert all(j["idValue"] != "DE0007164600" for call in figi.calls for j in call)


def test_only_priced_members_enter_the_universe():
    db = _memory_db()
    now = datetime.now(UTC)
    db.add_all([
        IndexUniverseMember(index_key="msci_world", isin="A1", name="a", symbol="AAA", weight=1.0, updated_at=now),
        IndexUniverseMember(index_key="msci_world", isin="B1", name="b", symbol="BBB.SI", weight=2.0,
                            symbol_ok=False, updated_at=now),
        IndexUniverseMember(index_key="msci_world", isin="C1", name="c", symbol=None, updated_at=now),
    ])
    db.commit()
    assert universe._msci_world_symbols(db) == ["AAA"]


def test_analyst_cache_reuses_fresh_successes_only():
    db = _memory_db()
    calls = []

    def fetch(symbol):
        calls.append(symbol)
        return {"ok": True, "provider": "yfinance", "data": {"target_mean": 10.0, "as_of": date(2026, 11, 1)}}

    first = analyst_cache.get_or_fetch(db, "abc", fetch)
    second = analyst_cache.get_or_fetch(db, "ABC", fetch)
    assert calls == ["abc"] and first["ok"] and second["data"]["target_mean"] == 10.0
    assert second["data"]["as_of"] == "2026-11-01"  # stored JSON-safe
    row = db.get(AnalystConsensusCache, "ABC")
    row.fetched_at = datetime.now(UTC) - analyst_cache.TTL - timedelta(minutes=1)
    db.commit()
    analyst_cache.get_or_fetch(db, "ABC", fetch)
    assert calls == ["abc", "ABC"]
    # A failure is not stored, so the next run asks again.
    analyst_cache.get_or_fetch(db, "NONE", lambda s: {"ok": False})
    assert db.get(AnalystConsensusCache, "NONE") is None


def test_prefetch_refreshes_stale_members_marks_unpriced_and_stops_on_time():
    db = _memory_db()
    old = datetime.now(UTC) - timedelta(days=10)
    db.add_all([
        IndexUniverseMember(index_key="msci_world", isin="A1", name="a", symbol="AAA", updated_at=old),
        IndexUniverseMember(index_key="msci_world", isin="B1", name="b", symbol="BAD.SI", updated_at=old),
    ])
    db.commit()
    refreshed, ingested, funds = [], [], []

    def refresh(session):
        refreshed.append(True)
        for m in session.query(IndexUniverseMember).all():
            m.updated_at = datetime.now(UTC)
        return {"members": 2}

    report = discover_universe_prefetch_inner(
        db, pace=0, refresh=refresh,
        ingest=lambda s: (ingested.append(s), (s, s != "BAD.SI"))[1],
        fundamentals=lambda s: funds.append(s),
        analyst=lambda s: {"ok": True, "data": {}},
        has_bars=lambda s: [],
    )
    assert refreshed and report["members_refresh"] == {"members": 2}
    assert "AAA" in ingested and "BAD.SI" in ingested and "AAPL" in ingested  # members and static lists
    assert db.query(IndexUniverseMember).filter_by(isin="B1").one().symbol_ok is False
    assert report["marked_unpriced"] == 1 and "BAD.SI" not in funds and "AAA" in funds
    # Bounded: with no time left it stops at once and says so.
    stopped = discover_universe_prefetch_inner(
        db, pace=0, max_seconds=-1, refresh=refresh, ingest=lambda s: (s, True),
        fundamentals=lambda s: None, analyst=lambda s: {"ok": False},
    )
    assert stopped["stopped_early"] and stopped["symbols"] == 0 and len(refreshed) == 1


def test_a_series_holding_the_last_weekday_is_not_fetched_again():
    from app.decision.discover.pipeline import _bars_current

    monday = date(2026, 11, 9)
    # _preingest_start returns the day before the last stored bar.
    assert _bars_current("2026-11-05", today=monday)          # last bar Friday 11-06
    assert not _bars_current("2026-11-04", today=monday)      # last bar Thursday
    assert _bars_current("2026-11-08", today=date(2026, 11, 10))  # Monday's bar on Tuesday
    assert not _bars_current("2026-11-07", today=date(2026, 11, 10))


def test_a_failed_fetch_of_a_priced_symbol_does_not_mark_it_unpriced():
    db = _memory_db()
    now = datetime.now(UTC)
    db.add(IndexUniverseMember(index_key="msci_world", isin="A1", name="a", symbol="AAA", updated_at=now))
    db.commit()
    report = discover_universe_prefetch_inner(
        db, pace=0, refresh=lambda s: {}, ingest=lambda s: (s, False),
        fundamentals=lambda s: None, analyst=lambda s: {"ok": False},
        has_bars=lambda s: [{"date": "2026-11-06", "close": 1.0}] if s == "AAA" else [],
    )
    assert report["marked_unpriced"] == 0
    assert db.query(IndexUniverseMember).one().symbol_ok is True
