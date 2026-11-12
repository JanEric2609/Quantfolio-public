"""WP11: the learning sleeve (ADR 0025 amendment 2026-11-12). Paper-record rules only; no broker write."""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from conftest import _memory_db

from app.decision import monthly_plan as mp
from app.foundation import capital_ladder as cl
from app.foundation.models.entities import (
    BrokerInstrument,
    LadderCandidate,
    LadderEvidenceDaily,
    PaperHolding,
    PaperPortfolio,
    User,
)
from app.foundation.settings import upsert_public_settings

TODAY = date(2026, 12, 3)
ISIN = "US0378331005"


def _setup(db, *, stage=1, e_harm=1.0, te=0.08, listed=True, setting=20):
    user = User(id="u", username="u", password_hash="x")
    db.add(user)
    db.add(PaperPortfolio(id="p", user_id="u", name="llm"))
    db.add(LadderCandidate(id="c", user_id="u", slot="llm", kind="advisor", ref_id="r", portfolio_id="p",
                           config_hash="h", epoch=1, pi=0.5, born_at=datetime(2026, 1, 2, tzinfo=UTC), stage=stage))
    db.add(LadderEvidenceDaily(candidate_id="c", day=date(2026, 11, 30), x=0.0, te=te, e_skill=2.0, e_harm=e_harm,
                               rel_drawdown=0.0, stage=stage))
    db.add(PaperHolding(portfolio_id="p", isin=ISIN, ticker="AAPL", name="Apple", asset_type="stock",
                        quantity=Decimal("10"), avg_buy_price=Decimal("100")))
    if listed:
        db.add(BrokerInstrument(source="trading212", ticker="AAPL_US_EQ", isin=ISIN, type="STOCK", currency="USD",
                                synced_at=datetime.now(UTC)))
    upsert_public_settings(db, {"ladder_learning_eur_per_month": setting})
    db.commit()


def _line(db):
    return mp.learning_line(db, "u", 20000.0, mp.plan_settings(db), today=TODAY)


def test_setting_zero_means_no_line():
    db = _memory_db()
    _setup(db, setting=0)
    out = _line(db)
    assert out["amount_eur"] == 0 and out["pick"] is None


def test_setting_above_the_cap_is_read_as_20():
    db = _memory_db()
    _setup(db, setting=50)
    out = cl.learning_amount_eur(db, "u", today=TODAY)
    assert out["amount_eur"] == 20.0 and out["estimate"] and out["not_tax_advice"] and out["not_financial_advice"]


def test_a_listed_paper_holding_becomes_the_pick():
    db = _memory_db()
    _setup(db)
    out = mp.learning_line(db, "u", 20000.0, mp.plan_settings(db), today=TODAY)
    assert out["pick"]["isin"] == ISIN and 0 < out["amount_eur"] <= 20.0


def test_harm_at_the_threshold_means_zero():
    db = _memory_db()
    _setup(db, e_harm=cl.HARM_THRESHOLD)
    assert cl.learning_amount_eur(db, "u", today=TODAY)["amount_eur"] == 0.0


def test_owner_veto_means_zero():
    db = _memory_db()
    _setup(db)
    upsert_public_settings(db, {"ladder_owner_veto": True})
    db.commit()
    assert cl.learning_amount_eur(db, "u", today=TODAY)["amount_eur"] == 0.0


def test_no_listed_instrument_means_no_line():
    db = _memory_db()
    _setup(db, listed=False)
    out = _line(db)
    assert out["amount_eur"] == 0.0 and out["pick"] is None


def test_the_authorisation_ends_after_12_plans():
    db = _memory_db()
    _setup(db)
    assert cl.learning_amount_eur(db, "u", today=date(2027, 10, 5))["amount_eur"] > 0  # plan 11 or 12
    assert cl.learning_amount_eur(db, "u", today=date(2028, 1, 5))["amount_eur"] == 0.0


def test_stage_two_is_funded_by_the_ladder_not_the_learning_sleeve():
    db = _memory_db()
    _setup(db, stage=2)
    assert cl.learning_amount_eur(db, "u", today=TODAY)["amount_eur"] == 0.0


def test_stage_one_amount_is_capped_by_the_tuition_weight():
    db = _memory_db()
    _setup(db, te=0.5)  # w1 = 0.01 / (0.03 + 0.82) = 1.2 % of the book
    out = mp.learning_line(db, "u", 100.0, mp.plan_settings(db), today=TODAY)  # 1.2 % of 100 EUR
    assert 0 < out["amount_eur"] < 2.0
