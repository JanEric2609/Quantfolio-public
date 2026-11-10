"""Second-source price checks, 8-K item codes and GDELT headlines (plan 2026-11-09)."""
from __future__ import annotations

import io
import json
import math
import zipfile
from datetime import UTC, date, datetime, timedelta

from conftest import _memory_db
from sqlalchemy import text

from app.foundation.data_backbone import cross_check as cc
from app.foundation.data_engineering import gdelt_news, sec_8k
from app.foundation.models.entities import (
    AppSetting,
    BarSeriesMeta,
    IndexUniverseMember,
    NewsMention,
    PriceCrossCheck,
    Sec8kEvent,
)


def _series(n: int = 16, start: float = 100.0, drift: float = 0.01) -> dict[str, float]:
    base = date(2026, 10, 1)
    return {(base + timedelta(days=i)).isoformat(): start * (1 + drift) ** i for i in range(n)}


# --- compare (pure) ----------------------------------------------------------------------


def test_agreeing_series_on_different_bases_are_ok():
    ref = _series()
    stored = {d: v * 0.97 for d, v in ref.items()}  # another provider's adjustment base
    out = cc.compare(stored, ref, tol=cc.TOL_US)
    assert out.verdict == "ok" and out.max_return_diff is not None and out.max_return_diff < 1e-9


def test_unadjusted_split_is_an_error():
    ref = _series()
    days = sorted(ref)
    stored = {d: (v * 4 if i < 8 else v) for i, (d, v) in enumerate((d, ref[d]) for d in days)}
    out = cc.compare(stored, ref, tol=cc.TOL_US)
    assert out.verdict == "error" and out.pattern == "split_unadjusted"
    assert out.detail["day"] == days[8] and math.isclose(out.detail["factor"], 0.25, rel_tol=1e-3)


def test_scale_x100_is_an_error():
    ref = _series()
    days = sorted(ref)
    stored = {d: (ref[d] * 100 if i >= 10 else ref[d]) for i, d in enumerate(days)}
    out = cc.compare(stored, ref, tol=cc.TOL_US)
    assert out.verdict == "error" and out.pattern == "scale_x100"


def test_one_day_spike_that_reverts_is_a_warning():
    ref = _series()
    days = sorted(ref)
    stored = dict(ref)
    stored[days[7]] = ref[days[7]] * 1.2
    out = cc.compare(stored, ref, tol=cc.TOL_US)
    assert out.verdict == "warn" and out.pattern == "spike"


def test_dividend_step_after_the_last_repull_is_drift():
    ref = _series()
    days = sorted(ref)
    # Stored bars before the ex-date were not restated yet: a 2 % step.
    stored = {d: (ref[d] * 1.02 if i < 9 else ref[d]) for i, d in enumerate(days)}
    out = cc.compare(stored, ref, tol=cc.TOL_US)
    assert out.verdict == "warn" and out.pattern == "dividend_mismatch"
    out = cc.compare(stored, ref, tol=cc.TOL_US, repulled_on=days[2])
    assert out.verdict == "ok" and out.pattern == "dividend_mismatch"


def test_stale_closes_are_a_warning():
    ref = _series()
    days = sorted(ref)
    stored = dict(ref)
    for d in days[10:13]:
        stored[d] = ref[days[9]]
    out = cc.compare(stored, ref, tol=cc.TOL_US)
    assert out.verdict == "warn" and out.pattern == "stale"


def test_too_few_common_days_is_no_reference():
    assert cc.compare(_series(4), _series(4), tol=cc.TOL_US).verdict == "no_reference"


def test_rotation_covers_every_symbol():
    symbols = [f"S{i}.DE" for i in range(50)]
    seen: set[str] = set()
    day = date(2026, 11, 1)
    for k in range(3):
        tonight = cc.rotation(symbols, day + timedelta(days=k), per_night=18)
        assert len(tonight) == 18
        seen |= set(tonight)
    assert seen == set(symbols)


# --- run_cross_check ---------------------------------------------------------------------


class _Provider:
    def __init__(self, name):
        self.name = name


class _Alpaca(_Provider):
    def __init__(self, data):
        super().__init__("alpaca")
        self.data = data
        self.feeds: list[str] = []

    def get_closes_multi(self, symbols, start, end=None, *, feed="sip"):
        self.feeds.append(feed)
        if feed == "sip":
            raise ValueError("Alpaca HTTP 403")
        return {s: self.data[s] for s in symbols if s in self.data}


class _Registry:
    def __init__(self, alpaca, history):
        self.providers = [alpaca, _Provider("tiingo"), _Provider("eod")]
        self.history = history
        self.asked: list[tuple[str, str]] = []

    def get_price_history(self, symbol, start=None, end=None, days=None, provider=None):
        self.asked.append((symbol, provider))
        rows = self.history.get((symbol, provider))
        if rows is None:
            return {"ok": False, "error": "none"}
        return {"ok": True, "data": [{"date": d, "close": c} for d, c in rows.items()]}


class _Ingester:
    def __init__(self):
        self.repulled: list[str] = []

    def split_events(self, symbol):
        return []

    def repull_series(self, symbol):
        self.repulled.append(symbol)


def _store(db, symbol, closes):
    db.add(BarSeriesMeta(symbol=symbol, provider="yfinance"))
    for d, c in closes.items():
        db.execute(
            text("INSERT INTO bar_prices (symbol, ts, open, high, low, close, volume, provider) "
                 "VALUES (:s, :ts, :c, :c, :c, :c, 0, 'yfinance')"),
            {"s": symbol, "ts": datetime.fromisoformat(d).replace(tzinfo=UTC), "c": c},
        )
    db.commit()


def test_cross_check_repulls_only_a_confirmed_error():
    db = _memory_db()
    today = date(2026, 10, 20)
    good = _series()
    days = sorted(good)
    split = {d: (v * 4 if i < 8 else v) for i, (d, v) in enumerate((d, good[d]) for d in days)}
    _store(db, "AAA", good)
    _store(db, "BBB", split)  # tie-break agrees: repull
    _store(db, "CCC", split)  # tie-break disagrees: left alone
    _store(db, "SAP.DE", good)
    alpaca = _Alpaca({"AAA": good, "BBB": good, "CCC": good})
    registry = _Registry(alpaca, {
        ("BBB", "tiingo"): good,
        ("CCC", "tiingo"): split,
        ("SAP.DE", "eod"): good,
    })
    ingester = _Ingester()
    stats = cc.run_cross_check(db, today=today, registry=registry, ingester=ingester)
    assert alpaca.feeds == ["sip", "iex"] and stats["feed"] == "iex"
    assert ingester.repulled == ["BBB"]
    rows = {(r.symbol, r.reference): r for r in db.query(PriceCrossCheck)}
    assert rows[("AAA", "alpaca")].verdict == "ok"
    assert rows[("BBB", "alpaca")].action == "repulled"
    assert rows[("CCC", "alpaca")].action == "tie_break_disagreed"
    assert rows[("SAP.DE", "eod")].verdict == "ok"
    # Re-running the same night updates rows rather than duplicating them.
    cc.run_cross_check(db, today=today, registry=registry, ingester=_Ingester())
    assert db.query(PriceCrossCheck).count() == 4


def test_twelvedata_probe_is_stored_once():
    db = _memory_db()
    registry = _Registry(_Alpaca({}), {("SPY", "twelvedata"): {"2023-12-29": 466.1}})
    registry.providers.append(_Provider("twelvedata"))
    probe = cc.probe_twelvedata(db, registry)
    assert probe is not None and probe["verified"] is True
    assert cc.probe_twelvedata(db, registry) is None  # already decided
    from app.foundation.providers.registry import _twelvedata_probe_passed

    assert _twelvedata_probe_passed(db) is True
    row = db.query(AppSetting).filter(AppSetting.key == "twelvedata_adjusted_probe_json").one()
    assert json.loads(row.value_json)["close"] == 466.1


# --- 8-K -----------------------------------------------------------------------------------


def test_8k_items_in_plain_words():
    assert sec_8k.material_items("2.02,4.02,9.01") == ["4.02"]
    assert sec_8k.describe("2.02,9.01") is None
    assert "auditor" in (sec_8k.describe("4.01") or "")


class _Sec:
    def __init__(self, responses):
        self.responses = responses
        self.urls: list[str] = []

    def get(self, url):
        self.urls.append(url)
        return self.responses.get(url, (404, b""))


def _index(rows):
    header = "Form Type   Company Name   CIK   Date Filed   File Name\n" + "-" * 80 + "\n"
    return (header + "\n".join(rows)).encode("latin-1")


def test_8k_refresh_stores_our_filings_with_items():
    db = _memory_db()
    db.add(IndexUniverseMember(index_key="msci_world", isin="US0378331005", name="APPLE INC", symbol="AAPL", symbol_ok=True))
    db.add(IndexUniverseMember(index_key="msci_world", isin="DE0007164600", name="SAP SE", symbol="SAP.DE", symbol_ok=True))
    db.commit()
    day = date(2026, 11, 6)  # a Friday
    tickers = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
               "1": {"cik_str": 1, "ticker": "OTHER", "title": "Other"}}
    idx = sec_8k.DAILY_INDEX.format(year=2026, quarter=4, day="20261106")
    submissions = {"filings": {"recent": {
        "accessionNumber": ["0000320193-26-000077", "0000320193-26-000070"],
        "form": ["8-K", "10-Q"],
        "items": ["4.02,9.01", ""],
        "acceptanceDateTime": ["2026-11-06T16:30:05.000Z", "2026-11-01T12:00:00.000Z"],
    }}}
    client = _Sec({
        sec_8k.TICKERS_URL: (200, json.dumps(tickers).encode()),
        idx: (200, _index([
            "8-K         APPLE INC                   320193      20261106    edgar/data/320193/0000320193-26-000077.txt",
            "8-K         OTHER CO                    1           20261106    edgar/data/1/0000000001-26-000001.txt",
            "4           APPLE INC                   320193      20261106    edgar/data/320193/0000320193-26-000078.txt",
        ])),
        sec_8k.SUBMISSIONS_URL.format(cik="0000320193"): (200, json.dumps(submissions).encode()),
    })
    from app.foundation.settings import upsert_public_settings

    upsert_public_settings(db, {sec_8k.THROUGH_KEY: "2026-11-05"})
    out = sec_8k.refresh_8k_events(db, client=client, today=day + timedelta(days=1))
    assert out["days"] == 1
    assert out["filings"] == 1 and out["material"] == 1
    row = db.query(Sec8kEvent).one()
    assert (row.symbol, row.items, row.filed_on) == ("AAPL", "4.02,9.01", day)
    # The next run starts after the last day read and asks for nothing already stored.
    client.urls.clear()
    sec_8k.refresh_8k_events(db, client=client, today=day + timedelta(days=1))
    assert idx not in client.urls
    text_ = sec_8k.recent_material_text(db, "AAPL", today=day + timedelta(days=3))
    assert text_ is not None and "can no longer be relied on" in text_ and "2026-11-06" in text_
    lines = sec_8k.events_between(db, ["AAPL"], datetime(2026, 11, 6, 12, tzinfo=UTC), datetime(2026, 11, 7, tzinfo=UTC))
    assert len(lines["AAPL"]) == 1


# --- GDELT ---------------------------------------------------------------------------------


def test_aliases_drop_legal_words_and_shared_names():
    assert gdelt_news.aliases("NESTLE SA") == {"nestle"}
    assert gdelt_news.aliases("LVMH MOET HENNESSY LOUIS VUITTON SE") >= {"lvmh"}
    assert "deutsche" not in gdelt_news.aliases("DEUTSCHE TELEKOM AG")
    index = gdelt_news.alias_index([("A.DE", "SIEMENS AG"), ("B.DE", "SIEMENS HEALTHINEERS AG"), ("C.PA", "Société Générale SA")])
    assert index["siemens"] == "A.DE" and index["siemens healthineers"] == "B.DE"
    assert index["societe generale"] == "C.PA"


def _row(when, orgs, title, url="https://x/1", outlet="example.com", tone="-2.5,1,3"):
    cols = [""] * 27
    cols[1], cols[3], cols[4], cols[13], cols[15] = when, outlet, url, orgs, tone
    cols[26] = f"<PAGE_TITLE>{title}</PAGE_TITLE>"
    return "\t".join(cols)


def _zip(lines):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("x.gkg.csv", "\n".join(lines))
    return buf.getvalue()


def test_gdelt_keeps_articles_about_the_company_once():
    db = _memory_db()
    for isin, name, sym in (("CH0038863350", "NESTLE SA", "NESN.SW"), ("DE0007164600", "SAP SE", "SAP.DE"),
                            ("US0378331005", "APPLE INC", "AAPL")):
        db.add(IndexUniverseMember(index_key="msci_world", isin=isin, name=name, symbol=sym, symbol_ok=True))
    db.commit()
    now = datetime(2026, 11, 10, 4, 0, tzinfo=UTC)
    file1 = _zip([
        _row("20261110031500", "nestle;unilever", "Nestlé cuts outlook again"),
        _row("20261110031500", "nestle", "Nestle cuts outlook again", url="https://y/2"),  # same story elsewhere
        _row("20261110031500", "nestle", "Food stocks fall"),  # named but not in the title
        _row("20261110031500", "sap", "SAP raises guidance"),
        _row("20261110031500", "sap", "Sap flow in maple trees"),  # three letters need capitals
        _row("20261110031500", "apple inc", "Apple unveils"),  # US: Finnhub covers it
    ])
    fetched: list[str] = []

    def fetch(url):
        fetched.append(url)
        return file1 if "20261110031500.gkg" in url else None

    out = gdelt_news.refresh_gdelt_news(db, now=now, fetch=fetch)
    assert out["mentions"] == 2
    rows = {(r.symbol, r.title) for r in db.query(NewsMention)}
    assert rows == {("NESN.SW", "Nestlé cuts outlook again"), ("SAP.DE", "SAP raises guidance")}
    assert db.query(NewsMention).filter_by(symbol="NESN.SW").one().gdelt_tone == -2.5
    # The next run starts where this one stopped.
    fetched.clear()
    gdelt_news.refresh_gdelt_news(db, now=now + timedelta(minutes=30), fetch=fetch)
    assert len(fetched) == 2 * 2 and db.query(NewsMention).count() == 2


def test_control_center_lists_unconfirmed_cross_check_errors():
    from app.foundation.control_center import attention_items

    db = _memory_db()
    today = datetime.now(UTC).date()
    db.add(PriceCrossCheck(symbol="BBB", checked_on=today, reference="alpaca", verdict="error",
                           pattern="split_unadjusted", action="tie_break_disagreed", detail_json={}))
    db.add(PriceCrossCheck(symbol="CCC", checked_on=today, reference="alpaca", verdict="error",
                           pattern="split_unadjusted", action="repulled", detail_json={}))
    db.commit()
    items = {i["id"]: i for i in attention_items(db)}
    assert "BBB" in items["prices-cross-check-error"]["detail"]
    assert "CCC" not in items["prices-cross-check-error"]["detail"]


def test_dossier_facts_cite_material_filings():
    from app.decision.discover.dossier_facts import build_facts

    facts = build_facts({}, annual={}, expected_return_pct=None, horizon_months=12, fundamentals={}, macro=None,
                        filings_text="8-K filings in the last 30 days: on 2026-11-06 it changed its auditor (8-K item 4.01)")
    assert any(f.key == "filings" and "auditor" in f.text for f in facts)
