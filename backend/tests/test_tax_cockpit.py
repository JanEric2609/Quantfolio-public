"""German tax cockpit estimate tests.

All outputs are estimates — the assertions encode the formulas, not legal truth.
"""
from datetime import date
from decimal import Decimal

import pytest
from conftest import _memory_db

from app.foundation.models.entities import TaxLot, User
from app.foundation import tax_cockpit
from app.foundation.tax_calc import (
    apply_sparer_pauschbetrag,
    apply_teilfreistellung,
    cap_foreign_wht,
    classify_loss_bucket,
    fifo_consume,
    teilfreistellung_pct_for_fund_class,
)
from app.foundation.tax_calc.allowances import kirchensteuer_amount, soli_amount
from app.foundation.tax_calc.vorabpauschale import basiszins_for_year, vorabpauschale_estimate


def _user(db):
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    return user


# ---------------------------------------------------------------------------
# Pure calculators
# ---------------------------------------------------------------------------


def test_teilfreistellung_rates_match_invstg_2018():
    assert teilfreistellung_pct_for_fund_class("aktien") == Decimal("0.30")
    assert teilfreistellung_pct_for_fund_class("misch") == Decimal("0.15")
    assert teilfreistellung_pct_for_fund_class("immobilien") == Decimal("0.60")
    assert teilfreistellung_pct_for_fund_class("other") == Decimal("0")
    assert teilfreistellung_pct_for_fund_class(None) == Decimal("0")


def test_apply_teilfreistellung_reduces_taxable_portion():
    assert apply_teilfreistellung(Decimal("1000"), Decimal("0.30")) == Decimal("700.00")
    assert apply_teilfreistellung(Decimal("1000"), Decimal("0.15")) == Decimal("850.00")
    assert apply_teilfreistellung(Decimal("1000"), Decimal("0")) == Decimal("1000")


def test_sparer_pauschbetrag_caps_at_allowance():
    taxable_after, used = apply_sparer_pauschbetrag(Decimal("600"), Decimal("1000"))
    assert taxable_after == Decimal("0")
    assert used == Decimal("600")

    taxable_after, used = apply_sparer_pauschbetrag(Decimal("1500"), Decimal("1000"))
    assert taxable_after == Decimal("500")
    assert used == Decimal("1000")

    # Spouse: 2000
    taxable_after, used = apply_sparer_pauschbetrag(Decimal("1500"), Decimal("2000"))
    assert taxable_after == Decimal("0")
    assert used == Decimal("1500")


def test_foreign_wht_cap_at_15pct():
    # 100€ gross dividend, 30€ foreign WHT (e.g. CH 35%) → cap creditable at 15%.
    result = cap_foreign_wht(Decimal("30"), Decimal("100"))
    assert result["creditable_eur"] == 15.0
    assert result["excess_eur"] == 15.0


def test_foreign_wht_country_cap_differs_from_default():
    # IE (Ireland) has a 10% cap per DBA
    result = cap_foreign_wht(Decimal("30"), Decimal("100"), country="IE")
    assert result["creditable_eur"] == 10.0  # 10% of 100
    assert result["excess_eur"] == 20.0
    assert result["treaty_cap_pct"] == 0.10


def test_foreign_wht_unknown_country_falls_back_to_15pct():
    # Unknown country → default 15%
    result = cap_foreign_wht(Decimal("30"), Decimal("100"), country="ZZ")
    assert result["creditable_eur"] == 15.0
    assert result["excess_eur"] == 15.0
    assert result["treaty_cap_pct"] == 0.15


def test_foreign_wht_none_country_falls_back_to_15pct():
    result = cap_foreign_wht(Decimal("30"), Decimal("100"), country=None)
    assert result["creditable_eur"] == 15.0
    assert result["excess_eur"] == 15.0


def test_classify_loss_bucket():
    assert classify_loss_bucket("stock") == "aktien"
    assert classify_loss_bucket("equity") == "aktien"
    assert classify_loss_bucket("etf") == "sonstige"
    assert classify_loss_bucket("bond") == "sonstige"
    assert classify_loss_bucket(None) == "sonstige"


def test_soli_and_church_tax_on_kest():
    # KESt = 250 → Soli = 13.75, Church (9%) = 22.50
    assert soli_amount(Decimal("250")) == Decimal("13.75")
    assert kirchensteuer_amount(Decimal("250"), Decimal("0.09")) == Decimal("22.50")
    assert kirchensteuer_amount(Decimal("250"), Decimal("0")) == Decimal("0")


def test_fifo_consume_across_multiple_lots():
    lots = [
        {"lot_id": "a", "acquired_at": date(2023, 1, 10), "quantity_remaining": Decimal("10"), "cost_basis_eur": Decimal("1000")},
        {"lot_id": "b", "acquired_at": date(2024, 6, 1), "quantity_remaining": Decimal("5"), "cost_basis_eur": Decimal("700")},
    ]
    # Sell 12 units at 150 EUR/unit, no fees. Cost basis consumed:
    #   10 units from lot a: 1000 EUR
    #   2 units from lot b: 700 * 2/5 = 280 EUR
    # Total basis = 1280 EUR. Proceeds = 1800 EUR. Realised gain = 520 EUR.
    sale = fifo_consume(lots, Decimal("12"), Decimal("150"))
    assert sale.proceeds_eur == Decimal("1800.00")
    assert sale.cost_basis_eur == Decimal("1280.00")
    assert sale.realised_gain_eur == Decimal("520.00")
    # Lot a fully consumed, lot b has 3 remaining with 420 EUR remaining basis.
    assert lots[0]["quantity_remaining"] == Decimal("0")
    assert lots[1]["quantity_remaining"] == Decimal("3")
    assert lots[1]["cost_basis_eur"] == Decimal("420")


def test_fifo_consume_insufficient_lots_raises():
    lots = [
        {"lot_id": "a", "acquired_at": date(2023, 1, 10), "quantity_remaining": Decimal("3"), "cost_basis_eur": Decimal("300")},
    ]
    with pytest.raises(ValueError):
        fifo_consume(lots, Decimal("10"), Decimal("100"))


def test_vorabpauschale_zero_when_fund_lost_value():
    result = vorabpauschale_estimate(
        fund_value_start_of_year_eur=Decimal("1000"),
        fund_value_end_of_year_eur=Decimal("950"),
        distributions_during_year_eur=Decimal("0"),
        tax_year=2025,
    )
    assert result["vorabpauschale_eur"] == 0.0


def test_vorabpauschale_capped_by_actual_gain():
    # Big basisertrag, small gain → capped at gain.
    result = vorabpauschale_estimate(
        fund_value_start_of_year_eur=Decimal("10000"),
        fund_value_end_of_year_eur=Decimal("10050"),
        distributions_during_year_eur=Decimal("0"),
        tax_year=2025,
    )
    # Basisertrag = 10000 * 0.0253 * 0.7 = 177.10 → but gain only 50 → cap at 50.
    assert result["vorabpauschale_eur"] == 50.0


def test_basiszins_for_year_2021_is_negative_and_known():
    # BMF-published Basiszins for 2021 was negative (§18 InvStG 2018 year).
    rate, known = basiszins_for_year(2021)
    assert rate == Decimal("-0.0045")
    assert known is True


def test_vorabpauschale_zero_for_negative_basiszins_year_but_reports_true_rate():
    # §18 Abs. 1 Satz 3 InvStG 2018: a negative Basiszins is floored at 0% for
    # computing Basisertrag, so no Vorabpauschale is levied for 2021 even
    # though the fund had a real gain. The true (negative) rate is still
    # surfaced in the result for transparency/display.
    result = vorabpauschale_estimate(
        fund_value_start_of_year_eur=Decimal("10000"),
        fund_value_end_of_year_eur=Decimal("10500"),
        distributions_during_year_eur=Decimal("0"),
        tax_year=2021,
    )
    assert result["vorabpauschale_eur"] == 0.0
    assert result["basiszins"] == -0.0045
    assert result["basiszins_known"] is True


def test_vorabpauschale_cap_adds_the_distributions_back():
    """Sec. 18(1) InvStG: Basisertrag less distributions, capped at price change
    plus distributions. 2026: 10,000 x 3.20 % x 0.7 = 224 Basisertrag."""
    result = vorabpauschale_estimate(
        fund_value_start_of_year_eur=Decimal("10000"),
        fund_value_end_of_year_eur=Decimal("10050"),
        distributions_during_year_eur=Decimal("100"),
        tax_year=2026,
    )
    # min(224 - 100, 50 + 100) = 124; the old min(224, 150) - 100 gave 50.
    assert result["vorabpauschale_eur"] == 124.0


def test_vorabpauschale_of_a_distributor_whose_price_fell():
    result = vorabpauschale_estimate(
        fund_value_start_of_year_eur=Decimal("10000"),
        fund_value_end_of_year_eur=Decimal("9950"),
        distributions_during_year_eur=Decimal("80"),
        tax_year=2026,
    )
    # Mehrbetrag -50 + 80 = 30 < 224 - 80 = 144.
    assert result["vorabpauschale_eur"] == 30.0


# ---------------------------------------------------------------------------
# End-to-end overview
# ---------------------------------------------------------------------------


def test_year_overview_combines_dividends_sales_and_allowance():
    db = _memory_db()
    user = _user(db)

    # Dividend: 500 EUR gross from an Aktien ETF (Teilfreistellung 30%) + 75 EUR US WHT.
    tax_cockpit.create_event(
        db,
        user.id,
        {
            "event_type": "dividend",
            "event_date": date(2026, 3, 15),
            "isin": "US0378331005",
            "name": "Apple Inc.",
            "fund_class": "aktien",
            "gross_eur": 500,
            "foreign_wht_eur": 75,
            "foreign_country": "US",
        },
    )

    # Lot + sale: 10 shares Aktien at 100€, sell 10 at 150€ → 500€ gain.
    tax_cockpit.create_lot(
        db,
        user.id,
        {
            "isin": "DE0001234567",
            "acquired_at": date(2024, 1, 10),
            "quantity": 10,
            "cost_basis_eur": 1000,
            "fund_class": "aktien",
        },
    )
    tax_cockpit.record_sale_via_fifo(
        db,
        user.id,
        isin="DE0001234567",
        sell_date=date(2026, 6, 1),
        sell_quantity=Decimal("10"),
        sell_price_per_unit_eur=Decimal("150"),
        fund_class="aktien",
        bucket="aktien",
    )

    overview = tax_cockpit.compute_year_overview(db, user.id, 2026)

    assert overview["estimate"] is True
    assert overview["lines"]["dividends_gross_eur"] == 500.0
    assert overview["lines"]["dividends_taxable_eur"] == 350.0  # 500 * 0.70
    # Sale realised gain 500€ * (1 - 0.30 Teilfreistellung) = 350€ taxable.
    assert overview["lines"]["realised_gain_aktien_eur"] == 350.0
    # Foreign WHT cap: 15% of 500€ = 75€ creditable (all of it).
    assert overview["lines"]["foreign_wht_creditable_eur"] == 75.0
    # Allowance default 1000€ absorbs all 700€ taxable income.
    assert overview["lines"]["taxable_income_after_allowance_eur"] == 0.0
    assert overview["tax"]["kest_due_eur"] == 0.0


def test_church_tax_reduces_effective_kest_in_overview():
    # §32d(1) EStG: with church tax owed, the effective KESt drops below flat 25%
    # (issue #140). 10_000€ aktien dividend → 7_000€ taxable, minus 1_000€
    # allowance = 6_000€ base. Flat KESt would be 1_500€; with 9% church it is
    # 6000 / 4.09 = 1466.99€.
    from app.foundation.settings import upsert_public_settings

    db = _memory_db()
    user = _user(db)
    upsert_public_settings(db, {"church_tax": "9"})

    tax_cockpit.create_event(
        db,
        user.id,
        {
            "event_type": "dividend",
            "event_date": date(2026, 3, 15),
            "isin": "DE0007164600",
            "name": "SAP SE",
            "fund_class": "aktien",
            "gross_eur": 10000,
            "foreign_wht_eur": 0,
        },
    )

    overview = tax_cockpit.compute_year_overview(db, user.id, 2026)

    assert overview["lines"]["taxable_income_after_allowance_eur"] == 6000.0
    # Gross flat KESt still reported for display.
    assert overview["tax"]["kest_due_eur"] == 1500.0
    # Effective KESt reduced by the deductible church tax.
    assert overview["tax"]["kest_after_foreign_wht_eur"] == 1466.99
    assert overview["tax"]["church_tax_rate"] == 0.09
    # Church tax charged on the reduced KESt: 1466.99 * 0.09 = 132.03.
    assert overview["tax"]["church_tax_eur"] == 132.03


def test_seed_lots_from_activity_creates_lots():
    from app.foundation.models.entities import ActivityLedgerEntry

    db = _memory_db()
    user = _user(db)
    db.add(
        ActivityLedgerEntry(
            user_id=user.id,
            source="manual",
            dedupe_hash="h1",
            activity_type="buy",
            date=date(2024, 1, 1),
            amount=Decimal("-1000"),
            description="Buy 10 shares",
            isin="US0378331005",
            quantity=Decimal("10"),
            price=Decimal("100"),
            fees=Decimal("0"),
        )
    )
    db.commit()
    created = tax_cockpit.seed_lots_from_activity(db, user.id)
    assert created == 1
    lots = db.query(TaxLot).filter(TaxLot.user_id == user.id).all()
    assert len(lots) == 1
    assert lots[0].quantity_remaining == Decimal("10")
    # Re-seed is idempotent (source_ref unique per activity).
    again = tax_cockpit.seed_lots_from_activity(db, user.id)
    assert again == 0


# ---------------------------------------------------------------------------
# Vorabpauschale auto-estimate from holdings
# ---------------------------------------------------------------------------


def test_vorabpauschale_auto_estimate_from_fund_holdings(monkeypatch):
    from datetime import datetime, timezone

    from app.foundation.models.entities import Holding, Portfolio, PriceCache

    # Pin the etf_universe evidence source to empty: this test exercises the
    # NO-evidence floor and must not depend on whether a local justETF cache
    # happens to exist (todo 22 added that evidence path).
    monkeypatch.setattr(tax_cockpit, "_load_etf_universe_index", lambda: {})

    db = _memory_db()
    user = _user(db)
    pf = Portfolio(user_id=user.id, name="Main")
    db.add(pf)
    db.commit()

    # An accumulating equity ETF that rose during the year.
    db.add(
        Holding(
            portfolio_id=pf.id,
            isin="IE00B4L5Y983",
            ticker="IWDA",
            name="iShares Core MSCI World",
            asset_type="etf",
            quantity=Decimal("100"),
            avg_buy_price=Decimal("90"),
        )
    )
    # A plain stock — must NOT generate a Vorabpauschale.
    db.add(
        Holding(
            portfolio_id=pf.id,
            isin="US0378331005",
            ticker="AAPL",
            name="Apple",
            asset_type="stock",
            quantity=Decimal("10"),
            avg_buy_price=Decimal("100"),
        )
    )

    # Use a fixed past year with a known, positive Basiszins so the
    # vorabpauschale_eur > 0 assertion is deterministic regardless of when the
    # test runs (date.today().year could hit a year without configured Basiszins).
    year = 2024
    now = datetime.now(timezone.utc)
    db.add(PriceCache(ticker="IWDA", date=date(year, 1, 2), close=Decimal("100"), fetched_at=now))
    db.add(PriceCache(ticker="IWDA", date=date(year, 1, 3), close=Decimal("110"), fetched_at=now))
    db.add(PriceCache(ticker="AAPL", date=date(year, 1, 2), close=Decimal("100"), fetched_at=now))
    db.add(PriceCache(ticker="AAPL", date=date(year, 1, 3), close=Decimal("130"), fetched_at=now))
    db.commit()

    items, total = tax_cockpit._estimate_holdings_vorabpauschale(db, user.id, year)

    assert len(items) == 1  # only the ETF
    assert items[0]["ticker"] == "IWDA"
    # No TaxLot classification exists for this ISIN → conservative "other"
    # default (0% Teilfreistellung), NOT "aktien" (30%).
    assert items[0]["fund_class"] == "other"
    assert items[0]["teilfreistellung_pct"] == 0.0
    assert items[0]["vorabpauschale_eur"] > 0
    # No Teilfreistellung applied when unclassified: taxable == raw vorabpauschale.
    assert total > 0
    assert abs(total - Decimal(str(items[0]["vorabpauschale_eur"]))) < Decimal("0.01")


def test_vorabpauschale_auto_estimate_uses_taxlot_fund_class():
    """A holding whose TaxLot carries a real classification uses that rate,
    not the conservative "other" default (issue: fund_class was previously
    hardcoded to "aktien" for every holding)."""
    from datetime import datetime, timezone

    from app.foundation.models.entities import Holding, Portfolio, PriceCache

    db = _memory_db()
    user = _user(db)
    pf = Portfolio(user_id=user.id, name="Main")
    db.add(pf)
    db.commit()

    db.add(
        Holding(
            portfolio_id=pf.id,
            isin="LU0000000001",
            ticker="MISCH",
            name="Mixed Allocation Fund",
            asset_type="fund",
            quantity=Decimal("100"),
            avg_buy_price=Decimal("90"),
        )
    )
    # A TaxLot for the same ISIN carries the real "misch" classification
    # (15% Teilfreistellung), entered by the user.
    tax_cockpit.create_lot(
        db,
        user.id,
        {
            "isin": "LU0000000001",
            "acquired_at": date(2023, 1, 10),
            "quantity": 100,
            "cost_basis_eur": 9000,
            "fund_class": "misch",
        },
    )

    year = 2024
    now = datetime.now(timezone.utc)
    db.add(PriceCache(ticker="MISCH", date=date(year, 1, 2), close=Decimal("100"), fetched_at=now))
    db.add(PriceCache(ticker="MISCH", date=date(year, 1, 3), close=Decimal("110"), fetched_at=now))
    db.commit()

    items, total = tax_cockpit._estimate_holdings_vorabpauschale(db, user.id, year)

    assert len(items) == 1
    assert items[0]["fund_class"] == "misch"
    assert items[0]["teilfreistellung_pct"] == 0.15
    assert items[0]["vorabpauschale_eur"] > 0
    # Taxable after 15% Teilfreistellung == 0.85 * vorabpauschale.
    expected = Decimal(str(items[0]["vorabpauschale_eur"])) * Decimal("0.85")
    assert abs(total - expected) < Decimal("0.01")


# ---------------------------------------------------------------------------
# §20 Abs. 6 EStG loss offsetting
# ---------------------------------------------------------------------------
#
# Satz 4 ring-fences Aktien losses to Aktien gains. Sätze 1-3 put every other
# loss in one general pot that offsets all remaining Kapitalerträge — dividends,
# interest and Vorabpauschale included. A previous implementation clamped the
# general pot to sonstige gains only, over-stating taxable income (always against
# the user) for anyone who realised a fund or bond loss in a year with dividend
# income. These tests pin both halves of the rule.


def _sale_event(db, user, *, gross, realised, bucket, day=6):
    tax_cockpit.create_event(
        db,
        user.id,
        {
            "event_type": "sale",
            "event_date": date(2026, day, 1),
            "isin": "DE000TEST001" if bucket == "aktien" else "IE00TESTETF1",
            "gross_eur": gross,
            "realised_gain_eur": realised,
            "bucket": bucket,
        },
    )


def test_general_loss_offsets_dividends_and_interest():
    """§20 Abs. 6 Sätze 1-3: a fund loss reduces dividend and interest income."""
    db = _memory_db()
    user = _user(db)

    tax_cockpit.create_event(
        db,
        user.id,
        {
            "event_type": "dividend",
            "event_date": date(2026, 3, 15),
            "gross_eur": 2000,
        },
    )
    tax_cockpit.create_event(
        db,
        user.id,
        {"event_type": "interest", "event_date": date(2026, 4, 1), "gross_eur": 500},
    )
    # 5,000 EUR realised loss on a non-Aktien position (ETF/bond → general pot).
    _sale_event(db, user, gross=10000, realised=-5000, bucket="sonstige")

    overview = tax_cockpit.compute_year_overview(db, user.id, 2026)
    lines = overview["lines"]

    # 2,000 dividends + 500 interest = 2,500 of income, fully absorbed.
    assert lines["taxable_income_before_allowance_eur"] == 0.0
    assert lines["general_loss_offset_eur"] == 2500.0
    # The unused 2,500 of loss carries forward; nothing is silently discarded.
    assert overview["carryforward"]["sonstige_loss_eur"] == 2500.0
    assert overview["tax"]["kest_due_eur"] == 0.0
    assert overview["estimate"] is True
    assert overview["not_tax_advice"] is True


def test_aktien_losses_stay_ring_fenced_from_dividends():
    """§20 Abs. 6 Satz 4: a share loss may NOT offset dividends or interest."""
    db = _memory_db()
    user = _user(db)

    tax_cockpit.create_event(
        db,
        user.id,
        {
            "event_type": "dividend",
            "event_date": date(2026, 3, 15),
            "gross_eur": 2000,
        },
    )
    _sale_event(db, user, gross=10000, realised=-5000, bucket="aktien")

    overview = tax_cockpit.compute_year_overview(db, user.id, 2026)
    lines = overview["lines"]

    # The dividend stays fully taxable — the Aktien pot cannot reach it.
    assert lines["taxable_income_before_allowance_eur"] == 2000.0
    assert lines["general_loss_offset_eur"] == 0.0
    assert overview["carryforward"]["aktien_loss_eur"] == 5000.0
    assert overview["carryforward"]["sonstige_loss_eur"] == 0.0


def test_general_loss_offsets_net_aktien_gain():
    """The general pot reaches Aktien *gains* — only Aktien losses are fenced."""
    db = _memory_db()
    user = _user(db)

    _sale_event(db, user, gross=5000, realised=3000, bucket="aktien", day=5)
    _sale_event(db, user, gross=5000, realised=-1000, bucket="sonstige", day=6)

    overview = tax_cockpit.compute_year_overview(db, user.id, 2026)
    lines = overview["lines"]

    assert lines["net_aktien_eur"] == 3000.0
    # 3,000 Aktien gain less the 1,000 general loss.
    assert lines["taxable_income_before_allowance_eur"] == 2000.0
    assert lines["general_loss_offset_eur"] == 1000.0
    assert overview["carryforward"]["sonstige_loss_eur"] == 0.0


def test_general_loss_never_produces_negative_taxable_income():
    """A loss larger than all income yields zero taxable income, not a negative."""
    db = _memory_db()
    user = _user(db)

    tax_cockpit.create_event(
        db,
        user.id,
        {"event_type": "interest", "event_date": date(2026, 4, 1), "gross_eur": 100},
    )
    _sale_event(db, user, gross=20000, realised=-9000, bucket="sonstige")

    overview = tax_cockpit.compute_year_overview(db, user.id, 2026)

    assert overview["lines"]["taxable_income_before_allowance_eur"] == 0.0
    assert overview["carryforward"]["sonstige_loss_eur"] == 8900.0
    assert overview["tax"]["total_tax_due_eur"] == 0.0


def test_vorabpauschale_credit_counts_years_received_before_the_sale():
    """Sec. 19(1) InvStG: bought March 2024, sold June 2026. The 2024 one (10/12,
    received 2 Jan 2025) and the 2025 one (received 2 Jan 2026) count; 2026's not yet."""
    from datetime import date as _d

    from app.foundation.tax_calc.jurisdictions.de.vorabpauschale import vorabpauschale_credit

    credit = vorabpauschale_credit(
        acquired_at=_d(2024, 3, 15), sold_at=_d(2026, 6, 1), quantity=Decimal("10"),
        per_unit_by_year={2024: Decimal("1.20"), 2025: Decimal("1.50"), 2026: Decimal("2.00")},
    )
    assert credit == Decimal("10") * (Decimal("1.20") * 10 / 12 + Decimal("1.50"))
    # Sold on 1 January 2026, before the 2025 one was received.
    early = vorabpauschale_credit(
        acquired_at=_d(2024, 3, 15), sold_at=_d(2026, 1, 1), quantity=Decimal("10"),
        per_unit_by_year={2024: Decimal("1.20"), 2025: Decimal("1.50")},
    )
    assert early == Decimal("10.00")


def test_a_fifo_fund_sale_deducts_the_vorabpauschale_already_taxed():
    """EUNL bought before 2024, 2024 rose 100 -> 120: Vorabpauschale per unit
    min(100 x 2.29 % x 0.7, 20) = 1.603. Selling 10 units in June 2025 credits 16.03."""
    from datetime import date as _d

    from app.foundation.models.entities import PriceCache

    db = _memory_db()
    user = _user(db)
    for day, close in ((_d(2024, 1, 2), "100"), (_d(2024, 12, 30), "120"), (_d(2025, 6, 2), "130")):
        db.add(PriceCache(ticker="EUNL.DE", date=day, close=Decimal(close), currency="EUR", source="test"))
    db.add(TaxLot(
        user_id=user.id, isin="IE00B4L5Y983", symbol="EUNL.DE", fund_class="aktienfonds",
        teilfreistellung_pct=Decimal("0.30"), acquired_at=_d(2023, 6, 1), quantity_initial=Decimal("10"),
        quantity_remaining=Decimal("10"), cost_basis_eur=Decimal("900"), source="manual",
    ))
    db.commit()
    event = tax_cockpit.record_sale_via_fifo(
        db, user.id, isin="IE00B4L5Y983", sell_date=_d(2025, 6, 2),
        sell_quantity=Decimal("10"), sell_price_per_unit_eur=Decimal("130"),
    )
    assert Decimal(str(event.realised_gain_eur)) == Decimal("400") - Decimal("16.03")
