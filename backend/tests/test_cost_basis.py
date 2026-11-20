"""Cost basis for the rebalance gain: MT535 parse, manual Einstandswert, DKB debit lots, per-depot gain."""
from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.dkb.adapter import _tolerant_mt535_acquisition_price
from app.foundation.models.entities import (
    ActivityLedgerEntry,
    DkbAccount,
    DkbPosition,
    TaxLot,
    User,
)
from app.foundation.portfolio import cost_basis


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _mt535(acq_lines: list[str]) -> list[str]:
    return [
        ":16R:FIN",
        ":35B:ISIN IE00B4L5Y983|/DE/A0RPWH|ISHSIII-CORE MSCI WORLD",
        ":90B::MRKT//ACTU/EUR131,50",
        ":98A::PRIC//20260930",
        ":93B::AGGR//UNIT/120,5",
        ":19A::HOLD//EUR15000,00",
        *acq_lines,
        ":16S:FIN",
        "-",
    ]


def _parse(lines):
    from fints.utils import MT535_Miniparser

    return MT535_Miniparser().parse(lines)


def test_library_parser_misses_the_bank_variant_and_the_tolerant_one_recovers_it():
    variant = [":70E::HOLD//1STK++++20231231+", "2123,45+EUR"]
    assert _parse(_mt535(variant))[0].acquisitionprice is None
    with _tolerant_mt535_acquisition_price():
        assert _parse(_mt535(variant))[0].acquisitionprice == pytest.approx(123.45)
    # restored afterwards: nothing leaks out of the context
    assert _parse(_mt535(variant))[0].acquisitionprice is None


def test_tolerant_parser_reads_the_line_dkb_actually_sends():
    # Published in python-fints issue #195 (a DKB depot): line 1 "1STK++++<date>",
    # line 2 "2<price>+<currency>". The leading 2 is the line number, so the
    # per-unit price is 40.5551139, not 240.55.
    dkb = [":70E::HOLD//1STK++++20250818", "240,5551139+EUR"]
    assert _parse(_mt535(dkb))[0].acquisitionprice is None
    with _tolerant_mt535_acquisition_price():
        assert _parse(_mt535(dkb))[0].acquisitionprice == pytest.approx(40.5551139)


def test_tolerant_parser_still_reads_the_original_clause_shape():
    original = [":70E::HOLD//1STK|223,968293+EUR"]
    with _tolerant_mt535_acquisition_price():
        assert _parse(_mt535(original))[0].acquisitionprice == pytest.approx(23.968293)


def test_unmatched_acquisition_clause_is_logged_at_debug(caplog):
    caplog.set_level("DEBUG", logger="app.foundation.dkb.adapter")
    with _tolerant_mt535_acquisition_price():
        _parse(_mt535([":70E::HOLD//garbage"]))
    assert any("unmatched acquisition clause" in r.message for r in caplog.records)


def test_parse_dkb_trade():
    text = "Depot 0123456789 Wertp.Abrechn. 07.04.2026 000001234567890 WKN A0RPWH Gesch.Art KV ISHSIII"
    assert cost_basis.parse_dkb_trade(text) == (date(2026, 4, 7), "A0RPWH", "KV")
    assert cost_basis.parse_dkb_trade("Miete April") is None


def _book(db, qty="120.5"):
    user = User(username="u", password_hash="x")
    db.add(user)
    db.commit()
    acct = DkbAccount(user_id=user.id, type="depot", iban="DE01", balance=Decimal("0"), currency="EUR")
    db.add(acct)
    db.commit()
    pos = DkbPosition(account_id=acct.id, isin="IE00B4L5Y983", ticker="EUNL.DE", name="MSCI World",
                      quantity=Decimal(qty), avg_buy_price=None, current_price=Decimal("131.5"),
                      current_value=Decimal(qty) * Decimal("131.5"))
    db.add(pos)
    db.commit()
    return user, acct, pos


def _debit(db, user, day, amount, wkn="A0RPWH", kind="KV", n=1):
    db.add(ActivityLedgerEntry(
        user_id=user.id, source="dkb", dedupe_hash=f"h{day}{n}", activity_type="cashflow", date=day,
        amount=Decimal(amount), description=f"Depot 1 Wertp.Abrechn. {day:%d.%m.%Y} 0001 WKN {wkn} Gesch.Art {kind} ISHSIII",
    ))
    db.commit()


def test_manual_cost_makes_an_opening_lot_and_replaces_on_reentry():
    db = _memory_db()
    user, acct, pos = _book(db)
    out = cost_basis.set_manual_cost(db, user.id, pos.id, Decimal("30000"))
    assert out and out["cost_basis_eur"] == 30000.0
    cost_basis.set_manual_cost(db, user.id, pos.id, Decimal("31000"))
    lots = db.query(TaxLot).filter(TaxLot.source == "manual").all()
    assert len(lots) == 1 and lots[0].cost_basis_eur == 31000 and lots[0].account_ref == acct.id
    from app.foundation.live_positions import live_positions

    lp = live_positions(db, user.id)
    dc = cost_basis.resolve_depot_costs(db, user.id, lp)[lp[0].id]
    assert dc.source == "lots" and dc.cost_eur == 31000
    assert cost_basis.set_manual_cost(db, "other-user", pos.id, Decimal("1")) is None
    assert cost_basis.clear_manual_cost(db, user.id, pos.id)
    assert db.query(TaxLot).count() == 0


def test_debit_lots_sit_on_top_of_the_opening_lot_and_split_the_gained_units():
    db = _memory_db()
    user, acct, pos = _book(db, qty="100")
    cost_basis.set_manual_cost(db, user.id, pos.id, Decimal("10000"))
    manual = db.query(TaxLot).one()
    manual.acquired_at = date(2026, 4, 1)
    db.commit()
    _debit(db, user, date(2026, 3, 20), "-251.50", n=1)  # before the entry: already in the opening lot
    _debit(db, user, date(2026, 5, 7), "-251.50", n=2)
    _debit(db, user, date(2026, 6, 8), "-501.50", n=3)
    _debit(db, user, date(2026, 6, 9), "-50", wkn="ZZZZZZ", n=4)  # unknown WKN: skipped
    pos.quantity = Decimal("110")  # +10 units since the entry
    db.commit()
    assert cost_basis.seed_dkb_debit_lots(db, user.id) == 2
    assert cost_basis.seed_dkb_debit_lots(db, user.id) == 2  # idempotent
    lots = db.query(TaxLot).filter(TaxLot.source == "dkb_debit").order_by(TaxLot.acquired_at).all()
    assert [float(lot.cost_basis_eur) for lot in lots] == [251.50, 501.50]
    assert sum(lot.quantity_remaining for lot in lots) == pytest.approx(Decimal("10"))
    assert float(lots[0].quantity_remaining) == pytest.approx(10 * 250 / 750)
    from app.foundation.live_positions import live_positions

    lp = live_positions(db, user.id)
    dc = cost_basis.resolve_depot_costs(db, user.id, lp)[lp[0].id]
    assert dc.source == "lots_estimated" and dc.cost_eur == Decimal("10753.00")


def test_a_sale_after_the_entry_stops_the_debit_lots():
    db = _memory_db()
    user, _acct, pos = _book(db, qty="100")
    cost_basis.set_manual_cost(db, user.id, pos.id, Decimal("10000"))
    db.query(TaxLot).one().acquired_at = date(2026, 4, 1)
    _debit(db, user, date(2026, 5, 7), "-251.50", n=1)
    _debit(db, user, date(2026, 6, 7), "500", kind="VK", n=2)
    pos.quantity = Decimal("110")
    db.commit()
    assert cost_basis.seed_dkb_debit_lots(db, user.id) == 0


def test_fifo_gain_takes_the_oldest_lot_first():
    from app.foundation.tax_calc.jurisdictions.de.gain_harvest import OpenLot

    lots = (
        OpenLot(date(2024, 1, 1), Decimal("10"), Decimal("1000")),  # 100 each
        OpenLot(date(2025, 1, 1), Decimal("10"), Decimal("1500")),  # 150 each
    )
    assert cost_basis.fifo_gain(lots, Decimal("5"), Decimal("200")) == Decimal("500")
    assert cost_basis.fifo_gain(lots, Decimal("15"), Decimal("200")) == Decimal("1000") + Decimal("250")


def _prices(rets):
    return (1 + rets).cumprod() * 100


def test_rebalance_gain_is_per_depot_not_all_or_nothing(monkeypatch):
    from app.foundation import tax_cockpit
    from app.foundation.models.entities import BrokerPosition, ConnectedAccount
    from app.foundation.portfolio import bridge
    from app.foundation.portfolio.metrics_wrappers import generate_rebalancing_suggestions

    db = _memory_db()
    user, _acct, pos = _book(db, qty="100")
    pos.current_value = Decimal("5000")
    pos.current_price = Decimal("50")
    # A second depot (Scalable) with the same ISIN, a known average cost, and a small other line.
    ca = ConnectedAccount(user_id=user.id, source="scalable", external_id="x", name="S")
    db.add(ca)
    db.commit()
    db.add(BrokerPosition(
        user_id=user.id, connected_account_id=ca.id, source="scalable", isin="IE00B4L5Y983", ticker="EUNL.DE",
        name="MSCI World", quantity=Decimal("100"), avg_buy_price=Decimal("25"), current_price=Decimal("50"),
        current_value=Decimal("5000"), currency="EUR",
    ))
    db.add(DkbPosition(account_id=_acct.id, isin="IE00BKM4GZ66", ticker="EIMI.L", name="EM", quantity=Decimal("10"),
                       avg_buy_price=Decimal("100"), current_price=Decimal("100"), current_value=Decimal("1000")))
    db.commit()
    rng = np.random.default_rng(1)
    rets = pd.DataFrame(rng.normal(0.0004, 0.01, (300, 2)), columns=["EUNL.DE", "EIMI.L"])
    monkeypatch.setattr(bridge, "build_real_price_matrix", lambda *a, **k: _prices(rets))
    monkeypatch.setattr(tax_cockpit, "_safe_etf_index", lambda: {})

    out = generate_rebalancing_suggestions(db, user.id, target="equal", contribution_eur=0.0, band_pp=5.0)
    assert out["estimate"] is True and out["not_tax_advice"] is True
    eunl = next(r for r in out["lines"] if r["ticker"] == "EUNL.DE")
    assert eunl["sell_eur"] > 0
    # Scalable half (cost 2,500 of 5,000) is known, the DKB half is not: partial, not None.
    assert eunl["gain_complete"] is False and eunl["cost_unknown_depots"] == ["DKB"]
    assert eunl["taxable_gain_eur"] == pytest.approx(eunl["sell_eur"] / 2 * 0.5, abs=0.01)
    dkb = next(d for d in eunl["depots"] if d["source"] == "dkb")
    assert dkb["can_enter_cost"] is True

    # Enter the DKB Einstandswert: now complete (FIFO lot, cost 4,000 of 5,000 -> 20 % gain).
    cost_basis.set_manual_cost(db, user.id, dkb["position_id"], Decimal("4000"))
    out = generate_rebalancing_suggestions(db, user.id, target="equal", contribution_eur=0.0, band_pp=5.0)
    eunl = next(r for r in out["lines"] if r["ticker"] == "EUNL.DE")
    assert eunl["gain_complete"] is True
    assert eunl["taxable_gain_eur"] == pytest.approx(eunl["sell_eur"] / 2 * (0.5 + 0.2), abs=0.01)
