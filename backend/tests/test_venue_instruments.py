"""WP3 (plan 2026-11-10): Xetra/Börse Frankfurt lists lift shares outside the EU and US to "medium"."""
from __future__ import annotations

from conftest import _memory_db

from app.decision.discover import tradeability
from app.foundation import venue_instruments
from app.foundation.models.entities import IndexUniverseMember, VenueInstrument

CSV = (
    "Market:;XFRA\n"
    "Date Last Update:;10.11.2026\n"
    "Product Status;Instrument Status;Instrument;ISIN;Product ID\n"
    "Active;Active;MITSUBISHI UFJ FIN.;JP3902900004;1\n"
    "Active;Active;SUMITOMO MITSUI FIN.;JP3890350006;1\n"
    "Active;Active;JAPAN TOBACCO INC.;JP3726800000;1\n"
    "Active;Active;BANCO COMERCIAL PORT.;PTBCP0AM0015;1\n"
    "Active;Inactive;DELISTED CO;JP0000000003;1\n"
    "Active;Active;NOT AN ISIN;XX;1\n"
)
PAGE = '<a href="/resource/blob/1/abc/data/t7-xfra-BF-allTradableInstruments.csv">csv</a>'


def _fetch(url: str) -> str:
    return CSV if url.endswith(".csv") else PAGE


def _db_with_lists():
    db = _memory_db()
    assert venue_instruments.refresh(db, fetch=_fetch)["xfra"] == 4
    return db


def test_parse_keeps_active_valid_isins_and_the_file_date():
    as_of, rows = venue_instruments.parse_csv(CSV)
    assert str(as_of) == "2026-11-10"
    assert sorted(r[0] for r in rows) == ["JP3726800000", "JP3890350006", "JP3902900004", "PTBCP0AM0015"]


def test_a_listed_share_outside_eu_and_us_is_medium_an_unlisted_one_low():
    db = _db_with_lists()
    db.add(IndexUniverseMember(index_key="msci_world", isin="JP3902900004", name="MUFG", symbol="8306.T"))
    db.add(IndexUniverseMember(index_key="msci_world", isin="JP9999999990", name="Nobody", symbol="9999.T"))
    db.commit()
    listed = tradeability.assess(db, "8306.T", None, "MUFG", "equity")
    assert listed["likely_tradeable"] is True and listed["confidence"] == "medium"
    assert any("Listed in Frankfurt" in r for r in listed["reasons"])
    unlisted = tradeability.assess(db, "9999.T", None, "Nobody", "equity")
    assert unlisted["likely_tradeable"] is False
    assert any("No German listing found" in r for r in unlisted["reasons"])


def test_without_a_list_the_answer_stays_uncertain():
    db = _memory_db()
    out = tradeability.assess(db, "8306.T", "JP3902900004", "MUFG", "equity")
    assert out["likely_tradeable"] is False
    assert any("availability uncertain" in r for r in out["reasons"])
    assert VenueInstrument and not venue_instruments.VenueListings(db).loaded


def test_lisbon_and_irish_suffixes_count_as_eu_venues():
    db = _memory_db()
    assert tradeability.assess(db, "BCP.LS", None, "BCP", "equity")["likely_tradeable"] is True
    assert tradeability.assess(db, "RYA.IR", None, "Ryanair", "equity")["likely_tradeable"] is True
