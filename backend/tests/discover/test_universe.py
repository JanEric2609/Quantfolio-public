"""Tests for the config-driven Discover universe builder (advisor-loop PR1).

Acceptance (B1): lowering ``min_market_cap_eur`` admits a mid-cap fixture
ticker that the old 1B floor rejected, and the universe extends beyond the old
~60 large-cap set.
"""
import json
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

from conftest import _memory_db

from app.foundation.models.entities import Fundamental, User
from app.decision.discover import universe as universe_mod
from app.decision.discover.config import activate_config, create_config
from app.decision.discover.universe import _EU_MIDCAP, _FIXED_INCOME_SEED, build_universe

MIDCAP_TICKER = "WAF.DE"  # Siltronic — MDAX, ~400M-1B EUR band
LARGECAP_TICKER = "SAP.DE"


def _user(db, name="alice") -> User:
    user = User(id=uuid4().hex, username=name, password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_fundamentals(
    db, ticker: str, market_cap: float, volume: float, isin: str | None = None
) -> None:
    payload = {"market_cap": market_cap, "volume": volume, "name": ticker}
    if isin is not None:
        payload["isin"] = isin
    db.add(
        Fundamental(
            ticker=ticker,
            data_json=json.dumps(payload),
            source="test",
            fetched_at=datetime.now(UTC),
            stale=False,
        )
    )
    db.commit()


class _EmptyEtfProvider:
    def get_universe(self):
        return []


def _build(db, user):
    """Run build_universe with all network paths stubbed out."""
    with (
        patch.object(universe_mod, "_get_etf_provider", return_value=_EmptyEtfProvider()),
        patch.object(
            universe_mod,
            "_fetch_live_fundamentals_no_db",
            side_effect=lambda t, registry: (t, {}),
        ),
    ):
        return build_universe(db, user.id)


def test_midcap_fixture_is_in_niche_pool():
    assert MIDCAP_TICKER in _EU_MIDCAP


def test_default_floor_admits_midcap():
    """Default config (250M floor) admits a 400M mid-cap the old 1B floor rejected."""
    db = _memory_db()
    user = _user(db)
    _seed_fundamentals(db, MIDCAP_TICKER, market_cap=400_000_000, volume=500_000)
    _seed_fundamentals(db, LARGECAP_TICKER, market_cap=150_000_000_000, volume=1_000_000)

    candidates = _build(db, user)
    symbols = {c["symbol"] for c in candidates}
    assert MIDCAP_TICKER in symbols
    assert LARGECAP_TICKER in symbols


def test_old_one_billion_floor_rejects_midcap():
    """With the legacy 1B floor configured, the same mid-cap is rejected."""
    db = _memory_db()
    user = _user(db)
    _seed_fundamentals(db, MIDCAP_TICKER, market_cap=400_000_000, volume=500_000)
    _seed_fundamentals(db, LARGECAP_TICKER, market_cap=150_000_000_000, volume=1_000_000)

    cfg = create_config(
        db,
        "universe",
        {"universe_max_size": 200, "min_market_cap_eur": 1_000_000_000, "min_avg_volume": 100_000},
        description="legacy floor",
    )
    activate_config(db, cfg.id)

    candidates = _build(db, user)
    symbols = {c["symbol"] for c in candidates}
    assert MIDCAP_TICKER not in symbols
    assert LARGECAP_TICKER in symbols


def test_volume_floor_still_enforced():
    """Tradeability floor survives: a mid-cap with thin volume stays out."""
    db = _memory_db()
    user = _user(db)
    _seed_fundamentals(db, MIDCAP_TICKER, market_cap=400_000_000, volume=10_000)

    candidates = _build(db, user)
    symbols = {c["symbol"] for c in candidates}
    assert MIDCAP_TICKER not in symbols


class TestIsinBackfill:
    """Issue 4: screen_index candidates should carry the ISIN yfinance already
    returns, instead of hardcoding it to None."""

    def test_cached_fundamental_isin_flows_through_to_candidate(self):
        db = _memory_db()
        user = _user(db)
        _seed_fundamentals(
            db, LARGECAP_TICKER, market_cap=150_000_000_000, volume=1_000_000,
            isin="DE0007164600",
        )

        candidates = _build(db, user)
        cand = next(c for c in candidates if c["symbol"] == LARGECAP_TICKER)
        assert cand["isin"] == "DE0007164600"

    def test_live_fetch_isin_flows_through_and_persists(self):
        db = _memory_db()
        user = _user(db)

        def _fake_fetch(ticker, registry):
            return ticker, {
                "market_cap": 150_000_000_000,
                "volume": 1_000_000,
                "name": ticker,
                "isin": "DE0007164600",
            }

        with (
            patch.object(universe_mod, "_get_etf_provider", return_value=_EmptyEtfProvider()),
            patch.object(universe_mod, "_fetch_live_fundamentals_no_db", side_effect=_fake_fetch),
        ):
            candidates = build_universe(db, user.id)

        cand = next(c for c in candidates if c["symbol"] == LARGECAP_TICKER)
        assert cand["isin"] == "DE0007164600"

        row = db.query(Fundamental).filter(Fundamental.ticker == LARGECAP_TICKER).one()
        persisted = json.loads(row.data_json)
        assert persisted["isin"] == "DE0007164600"

    def test_yf_fundamentals_filter_surfaces_isin(self):
        fake_info = {
            "marketCap": 1_000_000_000,
            "averageVolume": 500_000,
            "longName": "Fake Corp",
            "isin": "DE0007164600",
        }

        class _FakeTicker:
            def __init__(self, symbol):
                self.info = fake_info

        with patch("yfinance.Ticker", _FakeTicker):
            result = universe_mod._yf_fundamentals_filter(LARGECAP_TICKER)

        assert result["isin"] == "DE0007164600"

    def test_registry_fundamentals_filter_surfaces_isin(self):
        class _FakeRegistry:
            def get_fundamentals(self, ticker):
                return {"ok": True, "data": {"market_cap": 1_000_000_000, "isin": "DE0007164600"}}

        result = universe_mod._registry_fundamentals_filter(LARGECAP_TICKER, _FakeRegistry())
        assert result["isin"] == "DE0007164600"


class TestFixedIncomeSeed:
    """F17: CBE3.L has no Xetra listing, so EtfUniverseProvider's justETF
    scrape (Xetra-only symbols) can never surface it — it must be seeded
    explicitly and always included, independent of the fundamentals floor."""

    def test_seed_present_even_with_empty_etf_provider(self):
        db = _memory_db()
        user = _user(db)
        candidates = _build(db, user)
        symbols = {c["symbol"] for c in candidates}
        assert "CBE3.L" in symbols

    def test_seed_candidate_has_bond_metadata(self):
        db = _memory_db()
        user = _user(db)
        candidates = _build(db, user)
        cbe3 = next(c for c in candidates if c["symbol"] == "CBE3.L")
        assert cbe3["isin"] == "IE00B3VTMJ91"
        assert cbe3["source"] == "screen_etf"
        assert cbe3["tf_class"] == "other"

    def test_seed_excluded_when_already_held(self):
        db = _memory_db()
        user = _user(db)
        from app.foundation.models.entities import Holding, Portfolio

        portfolio = Portfolio(id="p1", user_id=user.id, name="Main")
        db.add(portfolio)
        db.add(Holding(
            id="h1", portfolio_id="p1", ticker="CBE3.L", name="CBE3.L",
            quantity=10, avg_buy_price=100,
        ))
        db.commit()

        candidates = _build(db, user)
        symbols = {c["symbol"] for c in candidates}
        assert "CBE3.L" not in symbols

    def test_only_one_seed_entry_defined(self):
        # Sanity check on the fixture itself, not build_universe — catches
        # an accidental duplicate/typo'd entry in the seed list.
        symbols = [e["symbol"] for e in _FIXED_INCOME_SEED]
        assert symbols == sorted(set(symbols), key=symbols.index)


def _seed_with_currency(db, ticker, market_cap, currency, volume=1_000_000):
    db.add(Fundamental(
        ticker=ticker,
        data_json=json.dumps({"market_cap": market_cap, "volume": volume, "name": f"{ticker} Corp", "currency": currency}),
        source="test", fetched_at=datetime.now(UTC), stale=False,
    ))
    db.commit()


_RATES = {"EUR": 1.0, "USD": 0.9, "GBP": 1.2, "JPY": 0.006}


def test_the_largest_names_fill_the_pool_not_the_first_lists():
    # 2026-09-28: the cap ran out inside the STOXX 600 list, so no FTSE 100
    # or S&P 500-only name was ever evaluated, however large.
    db = _memory_db()
    user = _user(db)
    cfg = create_config(db, "universe", {"universe_max_size": 20, "min_market_cap_eur": 250_000_000,
                                         "min_avg_volume": 100_000}, description="small cap")
    activate_config(db, cfg.id)
    for ticker in universe_mod._DAX40[:20]:
        _seed_with_currency(db, ticker, 1_000_000_000, "EUR")
    _seed_with_currency(db, "SHEL.L", 170_000_000_000, "GBp")   # FTSE 100, GBP 170bn
    _seed_with_currency(db, "ETN", 120_000_000_000, "USD")      # S&P 500-only list

    with patch.object(universe_mod, "_eur_per_unit", lambda db, ccy, cache: _RATES.get(ccy)):
        candidates = _build(db, user)

    symbols = [c["symbol"] for c in candidates if c["source"] == "screen_index"]
    assert len(symbols) == 16  # 80% of universe_max_size
    assert symbols[:2] == ["SHEL.L", "ETN"]
    assert next(c for c in candidates if c["symbol"] == "SHEL.L")["name"] == "SHEL.L Corp"


def test_the_floor_is_applied_in_eur():
    db = _memory_db()
    user = _user(db)
    _seed_with_currency(db, "7203.T", 35_000_000_000_000, "JPY")  # ~EUR 210bn
    _seed_with_currency(db, "9989.T", 30_000_000_000, "JPY")      # ~EUR 180m, raw > 250m

    with patch.object(universe_mod, "_eur_per_unit", lambda db, ccy, cache: _RATES.get(ccy)):
        symbols = {c["symbol"] for c in _build(db, user)}

    assert "7203.T" in symbols
    assert "9989.T" not in symbols


def test_a_listing_without_an_eur_rate_is_left_out():
    db = _memory_db()
    user = _user(db)
    _seed_with_currency(db, "NOVO-B.CO", 1_100_000_000_000, "DKK")

    with patch.object(universe_mod, "_eur_per_unit", lambda db, ccy, cache: _RATES.get(ccy)):
        symbols = {c["symbol"] for c in _build(db, user)}

    assert "NOVO-B.CO" not in symbols


def test_index_lists_have_no_duplicates_and_eurostoxx_has_fifty():
    """The Euro Stoxx 50 sample had 43 entries including Intel and two
    Yahoo symbols that do not exist (2026-09-28)."""
    from app.decision.discover import universe

    lists = [
        universe._DAX40, universe._EUROSTOXX50, universe._SP100, universe._NASDAQ100,
        universe._EU_MIDCAP, universe._US_MIDCAP, universe._STOXX600, universe._FTSE100,
        universe._CAC40, universe._SP500, universe._NIKKEI225,
    ]
    for tickers in lists:
        assert len(tickers) == len(set(tickers))
    assert len(universe._EUROSTOXX50) == 50
    assert all("." in t for t in universe._EUROSTOXX50)  # no US listings
