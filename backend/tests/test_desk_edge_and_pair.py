"""Cost arithmetic on the card, the DAX/ES50 shadow pair, the journal rule tag and the predictor replay
(ADR 0027 amendment 2026-11-25). Display and research only: no registered rule changes."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from desk_helpers import NOW, make_user
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.desk import cfd, cfd_journal, edge_math, pair_shadow, prereg
from app.decision.desk.underlyings import ALIASES
from app.foundation.core.db import Base
from app.foundation.models.entities import DeskPosition
from app.lab import desk_replay as rp


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_break_even_hit_rate_matches_the_hand_calculation():
    assert edge_math.break_even_hit_rate(3, 25) == pytest.approx(0.56)
    assert edge_math.break_even_hit_rate(4, 25) == pytest.approx(0.58)
    assert edge_math.break_even_hit_rate(1, 0) is None


def test_trades_needed_matches_the_published_sample_sizes():
    assert edge_math.trades_needed(0.60) == 153
    assert 615 <= edge_math.trades_needed(0.55) <= 620
    assert edge_math.trades_needed(0.5) is None


def test_pair_risk_multiple():
    assert edge_math.pair_risk_multiple(0.9) == pytest.approx(1.949, abs=1e-3)
    assert edge_math.pair_risk_multiple(0.0) == pytest.approx(2 ** 0.5)
    assert edge_math.pair_risk_multiple(1.0) == pytest.approx(2.0)


def test_cost_line_states_share_and_break_even():
    line = edge_math.cost_line(Decimal(1), Decimal(60))
    assert line and "1.7%" in line and "50.8%" in line
    assert edge_math.cost_line(Decimal(1), Decimal(0)) is None


def _closed(db, user, key, net, direction="long", day_offset=0):
    u = ALIASES[key]
    at = NOW + timedelta(days=day_offset)
    p = DeskPosition(
        user_id=user.id, mode="paper", cell=cfd.CELL, signal="index_momentum", product_type="cfd", isin=u.isin,
        underlying_isin=u.isin, underlying_label=u.label, direction=direction, liquid=True,
        leverage_or_omega=Decimal(1), quantity=Decimal(1), entry_price=Decimal(100), entry_at=at,
        status="closed", net_eur=Decimal(net), prereg_hash=prereg.PREREG_HASH, would_take_live=False)
    db.add(p)
    return p


def test_pair_shadow_halves_a_same_side_day_and_ignores_single_days():
    db = _memory_db()
    user = make_user(db)
    _closed(db, user, "DAX", "-1.0")
    _closed(db, user, "ES50", "-3.0")
    _closed(db, user, "DAX", "2.0", day_offset=1)  # only one index fired that day
    _closed(db, user, "DAX", "1.0", direction="long", day_offset=2)
    _closed(db, user, "ES50", "1.0", direction="short", day_offset=2)  # opposite sides: not the same factor bet
    db.commit()
    rep = pair_shadow.report(db)
    assert rep["days_both_fired"] == 2 and rep["days_same_side"] == 1
    assert rep["mean_full_eur"] == pytest.approx(-4.0) and rep["mean_half_eur"] == pytest.approx(-2.0)
    assert rep["estimate"] is True and rep["not_tax_advice"] is True


def test_journal_rule_defaults_to_registered_and_other_is_set_by_owner_only_input():
    db = _memory_db()
    user = make_user(db)
    u = ALIASES["DAX"]
    p = cfd.record_signal(db, user.id, "DAX", u, "long", NOW + timedelta(minutes=27), NOW, Decimal(100), Decimal("0.01"))
    db.commit()
    j = cfd_journal.for_position(db, p.id)
    assert j.rule == "registered"
    cfd_journal.set_rule(db, j, "other")
    assert j.rule == "other"
    with pytest.raises(cfd_journal.JournalError):
        cfd_journal.set_rule(db, j, "bogus")


def test_days_carry_the_rest_of_day_return():
    ny = rp.NEW_YORK

    def bars(day, o, pre, last):
        return [{"t": datetime(2026, 3, day, h, m, tzinfo=ny), "c": c} for h, m, c in
                ((9, 30, o), (15, 0, pre), (15, 30, last))]

    days = rp.days_from_bars(bars(2, 100.0, 100.0, 100.0) + bars(3, 101.0, 102.0, 103.0))
    assert len(days) == 1
    assert days[0].r_rod == pytest.approx(0.02) and days[0].r1 == pytest.approx(0.01)
    assert days[0].r_last == pytest.approx(103.0 / 102.0 - 1)


def test_rod_predictor_trades_the_sign_of_rod_not_of_r1():
    day = rp.Day(rp.date(2026, 3, 3), r1=0.01, r_last=0.002, sigma=0.01, friday_or_preholiday=False, r_rod=-0.01)
    r1 = rp.trade_results(day, rp.Costs(), "r1")
    rod = rp.trade_results(day, rp.Costs(), "rod")
    assert r1["gross_bp"] == pytest.approx(20.0) and rod["gross_bp"] == pytest.approx(-20.0)
    none = rp.Day(rp.date(2026, 3, 3), 0.01, 0.002, 0.01, False)
    assert rp.trade_results(none, rp.Costs(), "rod") is None


def test_evaluate_predictors_splits_into_terciles_with_holm():
    days = []
    for i in range(90):
        sigma = 0.005 + 0.0001 * i
        sign = 1 if i % 2 else -1
        days.append(rp.Day(rp.date(2026, 1, 1) + timedelta(days=i), sign * 0.003, sign * 0.0004 * (1 + i % 3),
                           sigma, False, sign * 0.004))
    out = rp.evaluate_predictors(days, rp.Costs(), desk_days=False)
    assert set(out["predictors"]) == {"r1", "rod"}
    terciles = out["predictors"]["rod"]["terciles"]
    assert set(terciles) == {"low", "mid", "high"}
    assert sum(t["gross_bp"]["n"] for t in terciles.values()) == 90
    assert all("p_holm" in t for t in terciles.values())


def test_cfd_rule_command_needs_an_id_when_several_trades_are_open(monkeypatch):
    from app.decision.desk import telegram
    from test_desk_cfd_journal import _Account, _signal

    db = _memory_db()
    user = make_user(db)
    log = []
    monkeypatch.setattr(telegram, "_say", lambda db_, chat, text, markup=None: log.append(text))
    monkeypatch.setattr(telegram, "_is_admin", lambda db_, a: True)
    monkeypatch.setattr(telegram, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: NOW)}))
    p1, j1 = _signal(db, user, "DAX")
    j1.created_at = NOW
    telegram.handle_cfd(db, _Account(user.id), "1", "/cfd rule other")
    assert j1.rule == "other" and "other rule" in log[-1]
    p2, j2 = _signal(db, user, "ES50", index="6000")
    j2.created_at = NOW
    telegram.handle_cfd(db, _Account(user.id), "1", "/cfd rule registered")
    assert j1.rule == "other" and j2.rule == "registered" and "Name the trade" in log[-1]
    telegram.handle_cfd(db, _Account(user.id), "1", f"/cfd rule registered {p1.id[:8]}")
    assert j1.rule == "registered"


def test_trades_needed_against_the_break_even_null():
    assert 610 <= edge_math.trades_needed(0.56, null=0.51) <= 620
    assert edge_math.trades_needed(0.5, null=0.51) is None
