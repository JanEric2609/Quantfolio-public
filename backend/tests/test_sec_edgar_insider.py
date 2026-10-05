"""Daily and quarterly refresh of the insider-trading panel from SEC EDGAR.

The panel was a one-off extract ending 2024-03-29, so Discover's insider
signal had been "unknown" for every US name since. A day ingested from the
daily index matched SEC's quarterly set for that day on all 2,054 rows once
amounts were rounded as the quarterly sets round them.
"""
import io
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from app.foundation.data_engineering import sec_edgar_insider as edgar
from app.foundation.data_engineering._pit_duckdb import insider_trading_coverage
from app.foundation.data_engineering.insider_trading_loader import read_insider_trading

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sec_edgar_form345_sample"

INDEX = """Description:           Daily Index of EDGAR Dissemination Feed by Form Type
Last Data Received:    Sep 25, 2026

Form Type   Company Name                                                  CIK         Date Filed  File Name
---------------------------------------------------------------------------------------------------------
10-K             Some Corp                                                     111         20260925    edgar/data/111/0000000111-26-000001.txt
4                ABBOTT LABORATORIES                                           1800        20260925    edgar/data/1800/0001306119-26-000009.txt
4                Conroy Kevin T                                                1306119     20260925    edgar/data/1800/0001306119-26-000009.txt
4/A              JOINT CO                                                      999999      20260925    edgar/data/999999/0000999999-26-000002.txt
"""

FORM4 = """<SEC-DOCUMENT>0001306119-26-000009.txt : 20260925
<XML>
<ownershipDocument>
    <documentType>4</documentType>
    <issuer>
        <issuerCik>0000001800</issuerCik>
        <issuerName>ABBOTT LABORATORIES</issuerName>
        <issuerTradingSymbol>abt</issuerTradingSymbol>
    </issuer>
    <reportingOwner>
        <reportingOwnerId><rptOwnerCik>0001306119</rptOwnerCik><rptOwnerName>Conroy Kevin T</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship><isDirector>true</isDirector><isOfficer>1</isOfficer></reportingOwnerRelationship>
    </reportingOwner>
    <reportingOwner>
        <reportingOwnerId><rptOwnerCik>1306120</rptOwnerCik><rptOwnerName>Conroy Trust</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship><isTenPercentOwner>true</isTenPercentOwner></reportingOwnerRelationship>
    </reportingOwner>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <transactionDate><value>2026-09-23</value></transactionDate>
            <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
            <transactionAmounts>
                <transactionShares><value>273.5516</value></transactionShares>
                <transactionPricePerShare><value>300.485</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
            <postTransactionAmounts><sharesOwnedFollowingTransaction><value>1,000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
            <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
        </nonDerivativeTransaction>
        <nonDerivativeTransaction>
            <transactionDate><value>2026-09-27</value></transactionDate>
            <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
            <transactionAmounts><transactionShares><value>10</value></transactionShares></transactionAmounts>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
</ownershipDocument>
</XML>
</SEC-DOCUMENT>"""


class FakeSec:
    def __init__(self, responses: dict[str, tuple[int, bytes]]):
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str) -> tuple[int, bytes]:
        self.calls.append(url)
        return self.responses.get(url, (404, b""))


@pytest.fixture
def panel(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    return tmp_path / "insider_trading_pit"


def _index_url(day: date) -> str:
    return edgar.DAILY_INDEX.format(year=day.year, quarter=(day.month - 1) // 3 + 1, day=f"{day:%Y%m%d}")


def test_the_daily_index_gives_one_entry_per_form_4_filing():
    entries = edgar.parse_form_index(INDEX)
    assert [(e.form_type, e.path) for e in entries] == [
        ("4", "edgar/data/1800/0001306119-26-000009.txt"),
        ("4/A", "edgar/data/999999/0000999999-26-000002.txt"),
    ]
    assert entries[0].filed == date(2026, 9, 25) and entries[0].cik == "0000001800"


def test_a_form_4_gives_one_row_per_transaction_and_owner():
    rows = edgar.parse_ownership_document(FORM4, date(2026, 9, 25))

    assert len(rows) == 4  # 2 transactions x 2 joint filers
    first = rows[0]
    assert first["issuer_cik"] == "0000001800" and first["ticker"] == "ABT"
    assert first["cik"] == "0001306119" and first["insider_role"] == "DIRECTOR,OFFICER"
    assert rows[1]["cik"] == "0001306120" and rows[1]["insider_role"] == "TENPERCENTOWNER"
    # Rounded half-up to 2 decimals, as SEC's quarterly sets store amounts.
    assert first["shares"] == 273.55 and first["price_per_share"] == 300.49
    assert first["shares_held_after"] == 1000.0
    assert first["filing_date"] == "2026-09-25" and first["transaction_date"] == "2026-09-23"


def test_the_10b5_1_checkbox_applies_to_every_row_of_the_filing():
    ticked = FORM4.replace("<documentType>4</documentType>", "<documentType>4</documentType>\n    <aff10b5One>1</aff10b5One>")
    unticked = FORM4.replace("<documentType>4</documentType>", "<documentType>4</documentType>\n    <aff10b5One>false</aff10b5One>")

    assert {r["plan_10b5_1"] for r in edgar.parse_ownership_document(ticked, date(2026, 9, 25))} == {True}
    assert {r["plan_10b5_1"] for r in edgar.parse_ownership_document(unticked, date(2026, 9, 25))} == {False}
    # Filings before April 2023 have no checkbox: unknown, not "no plan".
    assert {r["plan_10b5_1"] for r in edgar.parse_ownership_document(FORM4, date(2026, 9, 25))} == {None}
    frame = edgar._frame(edgar.parse_ownership_document(ticked, date(2026, 9, 25)))
    assert str(frame["plan_10b5_1"].dtype) == "boolean" and bool(frame["plan_10b5_1"].all())


def test_ingest_day_writes_a_validated_partition(panel):
    day = date(2026, 9, 25)
    sec = FakeSec({
        _index_url(day): (200, INDEX.encode()),
        edgar.ARCHIVE.format(path="edgar/data/1800/0001306119-26-000009.txt"): (200, FORM4.encode()),
    })
    report = edgar.InsiderRefreshReport()

    assert edgar.ingest_day(sec, day, report)  # type: ignore[arg-type]

    frame = read_insider_trading()
    # The sale dated after its own filing date is a filer's typo: dropped.
    assert len(frame) == 2 and set(frame["transaction_code"]) == {"P"}
    assert report.days_ingested == 1 and report.filings_failed == 1  # the 4/A returned 404
    assert (panel / "edgar_daily_20260925.parquet").exists()
    coverage = insider_trading_coverage(panel.parent)
    assert coverage is not None and coverage[1] == pd.Timestamp(day)


def test_no_index_yet_is_not_a_day_of_no_trading(panel):
    report = edgar.InsiderRefreshReport()
    assert not edgar.ingest_day(FakeSec({}), date(2026, 9, 28), report)  # type: ignore[arg-type]
    assert not panel.exists()


def test_quarterly_links_come_from_the_page_whatever_the_path():
    html = (
        '<a href="/files/datastandardsinnovation/data/insider-transactions-data-sets/2026q2_form345.zip">'
        '<a href="/files/structureddata/data/insider-transactions-data-sets/2026q1_form345.zip">'
    )
    assert edgar.quarterly_links(html) == {
        (2026, 2): "https://www.sec.gov/files/datastandardsinnovation/data/insider-transactions-data-sets/2026q2_form345.zip",
        (2026, 1): "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/2026q1_form345.zip",
    }


def _zip_fixture() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path in FIXTURE_DIR.iterdir():
            zf.write(path, path.name)
    return buf.getvalue()


def test_backfill_loads_missing_quarters_and_retires_their_daily_partitions(panel):
    panel.mkdir(parents=True)
    pd.DataFrame({"x": [1]}).to_parquet(panel / "edgar_daily_20240415.parquet")  # inside 2024 Q2
    pd.DataFrame({"x": [1]}).to_parquet(panel / "edgar_daily_20240702.parquet")  # after it
    page = '<a href="/files/x/2024q2_form345.zip"> <a href="/files/x/2024q1_form345.zip">'
    sec = FakeSec({
        edgar.DATASET_PAGE: (200, page.encode()),
        "https://www.sec.gov/files/x/2024q2_form345.zip": (200, _zip_fixture()),
    })
    report = edgar.InsiderRefreshReport()

    edgar.backfill_quarters(sec, report)  # type: ignore[arg-type]

    assert report.quarters_loaded == ["2024q2"]  # 2024 Q1 predates the backfill
    assert (panel / "2024q2_form345.parquet").exists()
    assert not (panel / "edgar_daily_20240415.parquet").exists()
    assert (panel / "edgar_daily_20240702.parquet").exists()
    assert edgar.loaded_quarters() == {(2024, 2)}


def test_days_to_ingest_follow_the_newest_quarter_and_skip_edgar_holidays(panel):
    panel.mkdir(parents=True)
    pd.DataFrame({"x": [1]}).to_parquet(panel / "2026q2_form345.parquet")
    pd.DataFrame({"x": [1]}).to_parquet(panel / "edgar_daily_20260701.parquet")

    days = edgar._days_to_ingest(date(2026, 7, 8))

    # Jul 1 is loaded, Jul 3 is Independence Day observed, Jul 4-5 a weekend.
    assert days == [date(2026, 7, 2), date(2026, 7, 6), date(2026, 7, 7)]


def test_refresh_stops_at_a_recent_day_that_is_not_published_yet(panel):
    panel.mkdir(parents=True)
    pd.DataFrame({"x": [1]}).to_parquet(panel / "2026q2_form345.parquet")
    day = date(2026, 7, 1)
    sec = FakeSec({
        edgar.DATASET_PAGE: (200, b""),
        _index_url(day): (200, INDEX.replace("20260925", "20260701").encode()),
        edgar.ARCHIVE.format(path="edgar/data/1800/0001306119-26-000009.txt"): (200, FORM4.encode()),
    })

    report = edgar.refresh_insider_trading(client=sec, today=date(2026, 7, 7))  # type: ignore[arg-type]

    assert report.days_ingested == 1
    assert not any("20260706" in url for url in sec.calls)  # stopped at Jul 2
