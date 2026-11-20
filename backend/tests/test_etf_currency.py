"""ETF currency look-through (foundation.etf_currency).

EUNL.DE is quoted in EUR on Xetra, but on 2026-09-28 73.3% of MSCI World
traded in USD and only 8.2% in EUR. Verification used to see a portfolio of
nothing but EUNL.DE as 100% EUR, and the "Dollar Slump" scenario left it
untouched.
"""
import io
import zipfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from conftest import _memory_db

from app.decision.verification.stress import PREDEFINED_SCENARIOS, _apply_scenario_to_holding
from app.foundation.etf_currency import (
    ETF_INDEX,
    SEED,
    STALE_AFTER,
    exposure_for_holding,
    holding_currency_exposure,
    parse_spdr_holdings,
    refresh_index_currency_weights,
)
from app.foundation.models.entities import Holding, IndexCurrencyWeights
from app.foundation.settings import upsert_public_settings

WORLD = "IE00B4L5Y983"  # iShares Core MSCI World (EUNL.DE / IWDA.L)
WORLD_HEUR = "IE000TB15RC6"  # UBS Core MSCI World hEUR
TODAY = date(2026, 10, 1)


def _xlsx(rows: list[list[str]]) -> bytes:
    """A minimal one-sheet workbook with inline-string cells."""
    def cell(ref: str, value: str) -> str:
        return f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>'

    body = "".join(
        f'<row r="{i + 1}">' + "".join(cell(f"{chr(65 + j)}{i + 1}", v) for j, v in enumerate(row)) + "</row>"
        for i, row in enumerate(rows)
    )
    sheet = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{body}</sheetData></worksheet>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as book:
        book.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


SPDR_FILE = _xlsx([
    ["Fund Name:", "SPDR MSCI World UCITS ETF (Acc)"],
    ["ISIN:", "IE00BFY0GT14"],
    ["Ticker Symbol:", "SPPW GY"],
    ["Holdings As Of:", "28-Sep-2026"],
    [],
    ["ISIN", "SEDOL", "Security Name", "Currency", "Number of Shares", "Percent of Fund"],
    ["US67066G1040", "2379504", "NVIDIA", "USD", "1", "60"],
    ["JP3633400001", "6900643", "Toyota", "JPY", "1", "10"],
    ["DE0007164600", "4846288", "SAP", "EUR", "1", "30"],
    ["", "", "Cash", "", "", ""],
])


def _holding(isin: str | None, ticker: str, asset_type: str = "etf") -> Holding:
    return Holding(
        id=uuid4().hex, portfolio_id="p", isin=isin, ticker=ticker, name=ticker, asset_type=asset_type,
        quantity=Decimal("1"), avg_buy_price=Decimal("100"), currency="EUR",
    )


def test_an_msci_world_etf_quoted_in_euros_is_mostly_dollars():
    db = _memory_db()
    exposure = exposure_for_holding(db, _holding(WORLD, "EUNL.DE"), today=TODAY)

    assert exposure.basis == "index_lookthrough" and exposure.index_key == "msci_world"
    assert exposure.weights["USD"] == pytest.approx(0.733, abs=1e-3)
    assert exposure.weights["EUR"] == pytest.approx(0.0819, abs=1e-3)
    assert sum(exposure.weights.values()) == pytest.approx(1.0)
    assert exposure.foreign_share("EUR") == pytest.approx(1 - 0.0819, abs=1e-3)
    assert not exposure.stale


def test_the_same_fund_on_another_listing_has_the_same_exposure():
    db = _memory_db()
    xetra = exposure_for_holding(db, _holding(WORLD, "EUNL.DE"), today=TODAY)
    london = exposure_for_holding(db, _holding(WORLD, "IWDA.L"), today=TODAY)
    assert xetra.weights == london.weights


def test_a_hedged_share_class_is_exposed_to_its_hedge_currency():
    db = _memory_db()
    exposure = exposure_for_holding(db, _holding(WORLD_HEUR, "WHEU.DE"), today=TODAY)
    assert exposure.basis == "hedged" and exposure.weights == {"EUR": 1.0}


def test_shares_and_unmapped_funds_keep_their_listing_currency():
    db = _memory_db()
    assert exposure_for_holding(db, _holding("US0378331005", "AAPL", "stock")).weights == {"USD": 1.0}
    unmapped = exposure_for_holding(db, _holding("IE00BYZK4552", "2B76.DE"))
    assert unmapped.basis == "listing" and unmapped.weights == {"EUR": 1.0}


def test_a_proxy_index_says_so():
    db = _memory_db()
    exposure = holding_currency_exposure(
        db, isin="IE00BK5BQT80", asset_type="etf", listing_currency="EUR", today=TODAY,  # VWCE
    )
    assert exposure.index_key == "ftse_all_world"
    assert exposure.weights["USD"] == pytest.approx(SEED["msci_acwi"]["USD"] / sum(SEED["msci_acwi"].values()))
    assert exposure.notes == ["ftse_all_world read through msci_acwi"]


def test_the_owner_can_map_another_etf():
    db = _memory_db()
    upsert_public_settings(db, {"etf_index_map": {"IE00BYZK4552": "sp500"}})
    exposure = holding_currency_exposure(db, isin="IE00BYZK4552", asset_type="etf", listing_currency="EUR")
    assert exposure.weights == {"USD": 1.0}


def test_stored_weights_replace_the_seed_and_age():
    db = _memory_db()
    db.add(IndexCurrencyWeights(
        index_key="msci_world", weights_json={"USD": 0.7, "EUR": 0.3}, as_of=date(2026, 1, 2),
        source="SPDR holdings sppw-gy", fetched_at=datetime(2026, 1, 3, tzinfo=UTC),
    ))
    db.commit()

    fresh = holding_currency_exposure(db, isin=WORLD, asset_type="etf", listing_currency="EUR", today=date(2026, 3, 1))
    assert fresh.weights == {"USD": pytest.approx(0.7), "EUR": pytest.approx(0.3)} and not fresh.stale

    later = date(2026, 1, 2) + STALE_AFTER + timedelta(days=1)
    assert holding_currency_exposure(db, isin=WORLD, asset_type="etf", listing_currency="EUR", today=later).stale


def test_a_single_currency_index_is_never_stale():
    exposure = holding_currency_exposure(
        None, isin="IE00B5BMR087", asset_type="etf", listing_currency="EUR", today=date(2030, 1, 1),  # CSPX
    )
    assert exposure.weights == {"USD": 1.0} and not exposure.stale


def test_parse_spdr_holdings_sums_weights_by_trading_currency():
    weights, as_of = parse_spdr_holdings(SPDR_FILE)
    assert as_of == date(2026, 9, 28)
    assert weights == {"USD": pytest.approx(0.6), "EUR": pytest.approx(0.3), "JPY": pytest.approx(0.1)}


def test_refresh_stores_each_index_and_survives_a_failure():
    db = _memory_db()

    def fetch(code: str) -> bytes:
        if code == "spym-gy":
            raise OSError("503")
        return SPDR_FILE

    report = refresh_index_currency_weights(db, fetch=fetch)

    assert "msci_em" in report["failed"] and "msci_world" in report["updated"]
    row = db.get(IndexCurrencyWeights, "msci_world")
    assert row is not None and row.as_of == date(2026, 9, 28) and row.weights_json["USD"] == pytest.approx(0.6)
    assert db.get(IndexCurrencyWeights, "msci_em") is None  # the seed still answers for it


def test_dollar_slump_hits_the_foreign_share_of_an_index_etf():
    """EUNL.DE used to escape every FX scenario because it is quoted in EUR."""
    db = _memory_db()
    slump = next(s for s in PREDEFINED_SCENARIOS if s.name == "Dollar Slump")
    shock = _apply_scenario_to_holding(_holding(WORLD, "EUNL.DE"), slump, "EUR", db)
    foreign = 1 - SEED["msci_world"]["EUR"] / sum(SEED["msci_world"].values())
    assert shock == pytest.approx(-slump.currency_shock_pct * foreign)


def test_every_mapped_isin_names_a_known_index():
    known = set(SEED) | {"ftse_all_world", "stoxx_europe_600"}
    for isin, target in ETF_INDEX.items():
        assert len(isin) == 12, isin
        assert target.startswith("hedged:") or target in known, (isin, target)


# --- country weights (the ETF page's geographic breakdown) --------------------

SPDR_FILE_WITH_COUNTRIES = _xlsx([
    ["Holdings As Of:", "29-Sep-2026"],
    [],
    ["ISIN", "Security Name", "Currency", "Percent of Fund", "Trade Country Name"],
    ["US67066G1040", "NVIDIA", "USD", "60", "United States"],
    ["NL0009805522", "Nebius", "USD", "10", "Netherlands"],
    ["DE0007164600", "SAP", "EUR", "30", "Germany"],
])


def test_the_holdings_file_gives_country_weights_too():
    """Currency and country differ: Nebius trades in USD but is Dutch."""
    from app.foundation.etf_currency import parse_spdr_file

    parsed = parse_spdr_file(SPDR_FILE_WITH_COUNTRIES)

    assert parsed.currencies == {"USD": pytest.approx(0.7), "EUR": pytest.approx(0.3)}
    assert parsed.countries == {
        "United States": pytest.approx(0.6), "Netherlands": pytest.approx(0.1), "Germany": pytest.approx(0.3),
    }
    assert parse_spdr_file(SPDR_FILE).countries is None  # a file without the column


def test_refresh_stores_countries_and_they_replace_the_seed():
    from app.foundation.etf_currency import etf_country_weights

    db = _memory_db()
    refresh_index_currency_weights(db, fetch=lambda code: SPDR_FILE_WITH_COUNTRIES)

    row = db.get(IndexCurrencyWeights, "msci_world")
    assert row is not None and row.country_weights_json["Netherlands"] == pytest.approx(0.1)
    countries = etf_country_weights(db, WORLD)
    assert countries is not None
    assert countries.weights["United States"] == pytest.approx(0.6)
    assert countries.as_of == date(2026, 9, 29) and countries.source == "SPDR holdings sppw-gy"


def test_country_weights_by_etf():
    from app.foundation.etf_currency import etf_country_weights

    world = etf_country_weights(None, WORLD)
    assert world is not None and 0.72 < world.weights["United States"] < 0.74  # seed, 2026-09-29
    assert etf_country_weights(None, "IE00B5BMR087").weights == {"United States": 1.0}  # S&P 500
    ftse = etf_country_weights(None, "IE00BK5BQT80")  # VWCE
    assert ftse is not None and ftse.read_through == "msci_acwi"
    # A hedged class is mapped to its hedge, the Nasdaq 100 spans several
    # countries with no file here, and an unmapped ETF has no index.
    assert etf_country_weights(None, WORLD_HEUR) is None
    assert etf_country_weights(None, "IE00B53SZB19") is None
    assert etf_country_weights(None, "IE0000000000") is None


def test_country_seeds_sum_to_one_and_name_known_indices():
    from app.foundation.etf_currency import COUNTRY_SEED

    for key, weights in COUNTRY_SEED.items():
        assert key in SEED, key
        assert sum(weights.values()) == pytest.approx(1.0, abs=0.002), key
