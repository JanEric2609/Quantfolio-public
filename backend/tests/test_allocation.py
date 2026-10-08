"""Covariance-only target allocations and band rebalancing."""
import numpy as np
import pandas as pd
import pytest

from app.foundation.allocation import (
    _cap_weights,
    allocate_contribution,
    allocation_candidates,
    drift_sales,
    portfolio_stats,
)


def _returns(seed=0, n=500):
    rng = np.random.default_rng(seed)
    vols = np.array([0.010, 0.012, 0.020])
    z = rng.standard_normal((n, 3))
    z[:, 1] = 0.9 * z[:, 0] + np.sqrt(1 - 0.81) * z[:, 1]  # A and B highly correlated
    idx = pd.bdate_range("2024-01-01", periods=n)
    return pd.DataFrame(z * vols, index=idx, columns=["A", "B", "C"])


def test_every_candidate_is_long_only_and_fully_invested():
    out = allocation_candidates(_returns(), {"A": 0.98, "B": 0.01, "C": 0.01})
    assert out["available"] and set(out["methods"]) == {"equal", "inverse_vol", "min_variance", "erc", "hrp"}
    for m in out["methods"].values():
        w = np.array(list(m["weights"].values()))
        assert w.min() >= -1e-9 and w.sum() == pytest.approx(1.0)
    assert 0 < out["covariance"]["shrinkage"] < 1


def test_erc_equalises_risk_contributions():
    out = allocation_candidates(_returns(), {})
    rc = list(out["methods"]["erc"]["risk_contributions"].values())
    assert max(rc) - min(rc) < 0.02
    assert sum(rc) == pytest.approx(1.0)


def test_min_variance_has_the_lowest_volatility():
    out = allocation_candidates(_returns(), {})
    vols = {k: m["volatility"] for k, m in out["methods"].items()}
    assert vols["min_variance"] == pytest.approx(min(vols.values()), rel=1e-4)


def test_turnover_is_half_the_absolute_change():
    cov = np.eye(2) * 0.04
    stats = portfolio_stats(np.array([0.5, 0.5]), cov, current=np.array([0.9, 0.1]))
    assert stats["turnover"] == pytest.approx(0.4)
    assert stats["effective_n"] == pytest.approx(2.0)


def test_a_cap_redistributes_the_excess():
    w = _cap_weights(np.array([0.7, 0.2, 0.1]), 0.5)
    assert w.max() <= 0.5 + 1e-12 and w.sum() == pytest.approx(1.0)
    assert w[1] / w[2] == pytest.approx(2.0)


def test_new_money_goes_to_the_most_underweight_first():
    split = allocate_contribution({"A": 900.0, "B": 100.0}, {"A": 50.0, "B": 50.0}, 200.0)
    assert split == {"A": 0.0, "B": 200.0}


def test_no_sale_inside_the_band_or_when_a_year_of_contributions_fixes_it():
    targets = {"A": 50.0, "B": 50.0}
    unlocked = {"A": True, "B": True}
    # 54 % vs 50 %: inside a 5 pp band.
    assert drift_sales({"A": 5400.0, "B": 4600.0}, targets, unlocked, 0.0, 5.0) == {}
    # 70 % but a year of 1,000 a month brings it back.
    assert drift_sales({"A": 7000.0, "B": 3000.0}, targets, unlocked, 1000.0, 5.0) == {}
    # 70 % with no contributions: sold down to 50 %.
    assert drift_sales({"A": 7000.0, "B": 3000.0}, targets, unlocked, 0.0, 5.0) == {"A": pytest.approx(2000.0)}


def test_real_book_rebalance_buys_underweights_and_shows_the_taxable_gain(monkeypatch):
    """A 90/10 book against equal weight, no contribution: the overweight line
    is sold to 50 % and the sale's gain is reported after Teilfreistellung."""
    from decimal import Decimal

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.foundation.core.db import Base
    from app.foundation.models.entities import DkbAccount, DkbPosition, User
    from app.foundation.portfolio import bridge
    from app.foundation.portfolio.metrics_wrappers import generate_rebalancing_suggestions

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    user = User(username="u", password_hash="x")
    db.add(user)
    db.commit()
    acct = DkbAccount(user_id=user.id, type="depot", iban="DE01", balance=Decimal("0"), currency="EUR")
    db.add(acct)
    db.commit()
    for isin, ticker, value, cost in (("IE00B4L5Y983", "EUNL.DE", "9000", "60"), ("IE00BKM4GZ66", "EIMI.L", "1000", "100")):
        db.add(DkbPosition(account_id=acct.id, isin=isin, ticker=ticker, name=ticker, quantity=Decimal("100"),
                           avg_buy_price=Decimal(cost), current_price=Decimal(value) / 100, current_value=Decimal(value)))
    db.commit()
    rets = _returns()[["A", "C"]].rename(columns={"A": "EUNL.DE", "C": "EIMI.L"})
    prices = (1 + rets).cumprod() * 100
    monkeypatch.setattr(bridge, "build_real_price_matrix", lambda *a, **k: prices)
    # No fund evidence: the conservative "other" class, 0 % Teilfreistellung.
    from app.foundation import tax_cockpit
    monkeypatch.setattr(tax_cockpit, "_safe_etf_index", lambda: {})

    out = generate_rebalancing_suggestions(db, user.id, target="equal", contribution_eur=0.0, band_pp=5.0)
    assert out["available"] and out["target_method"] == "equal"
    eunl = next(r for r in out["lines"] if r["ticker"] == "EUNL.DE")
    assert eunl["sell_eur"] == pytest.approx(4000.0)
    # Cost 6,000 on 9,000: a third of the sale is gain, all of it taxable here.
    assert eunl["taxable_gain_eur"] == pytest.approx(4000.0 / 3, abs=0.01)
    assert eunl["tax_eur"] == pytest.approx(4000.0 / 3 * 0.26375, abs=0.01)
    eimi = next(r for r in out["lines"] if r["ticker"] == "EIMI.L")
    assert eimi["buy_eur"] == pytest.approx(4000.0)

    # With 1,000 a month a year of contributions brings it to 9,000 / 22,000 = 41 %: no sale.
    out = generate_rebalancing_suggestions(db, user.id, target="equal", contribution_eur=1000.0, band_pp=5.0)
    assert not out["sells"]
    assert next(r for r in out["lines"] if r["ticker"] == "EIMI.L")["buy_eur"] == pytest.approx(1000.0)


# ---- new-money allocator ---------------------------------------------------

from app.foundation.allocation import history_span, new_money_plan, project_book  # noqa: E402


def test_new_money_plan_buys_the_underweight_line_and_sells_nothing():
    plan = new_money_plan(_returns(), {"A": 9000.0, "B": 0.0, "C": 0.0}, 300.0)
    assert plan["available"] and set(plan["plans"]) == {"min_variance", "erc", "hrp", "equal"}
    eq = plan["plans"]["equal"]
    assert all(v >= 0 for v in eq["eur_this_month"].values())
    assert sum(eq["eur_this_month"].values()) == pytest.approx(300.0)
    assert eq["eur_this_month"]["A"] == pytest.approx(0.0)  # already far above 1/3
    assert sum(eq["weights_after"].values()) == pytest.approx(1.0)
    assert eq["weights_after"]["A"] < plan["weights_current"]["A"]
    assert "months" in eq["summary"]


def test_project_book_converges_without_selling():
    book = project_book({"A": 100.0, "B": 0.0}, {"A": 50.0, "B": 50.0}, 100.0, 12)
    assert book["A"] == pytest.approx(100.0) or book["A"] > 99.9  # A never loses money
    assert sum(book.values()) == pytest.approx(1300.0)
    assert book["B"] > 500


def test_new_money_plan_with_empty_book_follows_the_target():
    plan = new_money_plan(_returns(), {}, 100.0, max_weight=0.5)
    erc = plan["plans"]["erc"]
    for a, w in erc["target_weights"].items():
        assert erc["eur_this_month"][a] == pytest.approx(100.0 * w)


def test_history_span_names_the_young_line():
    old = {f"2024-01-{d:02d}": 1.0 for d in range(1, 29)}
    young = {f"2024-01-{d:02d}": 1.0 for d in range(20, 29)}
    span = history_span({"OLD": old, "NEW": young})
    assert span["limited_by"] == "NEW" and span["days"] == 9
    assert history_span({"OLD": old})["limited_by"] is None


def test_real_book_rebalance_defaults_to_plan_sleeves_and_never_sells_core_into_a_stock():
    """97 % world core + EM core + a tiny stock: the core ETFs are one sleeve,
    the locked satellite (target 0 %) is neither bought nor sold."""
    from decimal import Decimal

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.decision.monthly_plan import sleeve_plan
    from app.foundation.core.db import Base
    from app.foundation.models.entities import DkbAccount, DkbPosition, User
    from app.foundation.portfolio.metrics_wrappers import generate_rebalancing_suggestions

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    user = User(username="u", password_hash="x")
    db.add(user)
    db.commit()
    acct = DkbAccount(user_id=user.id, type="depot", iban="DE01", balance=Decimal("0"), currency="EUR")
    db.add(acct)
    db.commit()
    for isin, ticker, value in (("IE00B4L5Y983", "EUNL.DE", "9700"), ("IE00BKM4GZ66", "EIMI.L", "100"), ("US67066G1040", "NVDA", "200")):
        db.add(DkbPosition(account_id=acct.id, isin=isin, ticker=ticker, name=ticker, quantity=Decimal("1"),
                           avg_buy_price=Decimal(value), current_price=Decimal(value), current_value=Decimal(value)))
    db.commit()
    plan = sleeve_plan(db, user.id)
    assert plan["classify"]("IE00BKM4GZ66") == "core" and plan["classify"]("US67066G1040") == "satellite"
    out = generate_rebalancing_suggestions(db, user.id, contribution_eur=100.0, sleeve_plan=plan)
    assert out["available"] and out["target_method"] == "sleeves" and "sleeves" in out["methods"]
    assert out["estimate"] is True and out["not_tax_advice"] is True
    assert not out["sells"] and all(r["sell_eur"] == 0 for r in out["lines"])
    nvda = next(r for r in out["lines"] if r["ticker"] == "NVDA")
    assert nvda["buy_eur"] == 0 and nvda["sleeve"] == "satellite"
    core = next(s for s in out["sleeves"] if s["key"] == "core")
    assert core["target_pct"] == 100.0


def _sleeve_book():
    from decimal import Decimal

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.foundation.core.db import Base
    from app.foundation.models.entities import DkbAccount, DkbPosition, User

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    user = User(username="u", password_hash="x")
    db.add(user)
    db.commit()
    acct = DkbAccount(user_id=user.id, type="depot", iban="DE01", balance=Decimal("0"), currency="EUR")
    db.add(acct)
    db.commit()
    for isin, ticker, value in (("IE00B4L5Y983", "EUNL.DE", "9400"), ("US67066G1040", "NVDA", "600")):
        db.add(DkbPosition(account_id=acct.id, isin=isin, ticker=ticker, name=ticker, quantity=Decimal("1"),
                           avg_buy_price=Decimal(value), current_price=Decimal(value), current_value=Decimal(value)))
    db.commit()
    return db, user


def test_sleeve_rebalance_takes_running_pick_plans_out_of_the_contribution(monkeypatch):
    from app.decision import monthly_plan
    from app.foundation.portfolio.metrics_wrappers import generate_rebalancing_suggestions
    from app.foundation.settings import upsert_public_settings

    db, user = _sleeve_book()
    upsert_public_settings(db, {"monthly_contribution_eur": 500})
    monkeypatch.setattr(monthly_plan, "running_savings_plans", lambda *a, **k: [
        {"running": True, "monthly_eur": 100.0, "sleeve": "satellite"}])
    plan = monthly_plan.sleeve_plan(db, user.id)
    assert plan["other_budget_eur"] == pytest.approx(100.0)
    out = generate_rebalancing_suggestions(db, user.id, sleeve_plan=plan)
    assert out["contribution_eur"] == pytest.approx(400.0)
    core = next(s for s in out["sleeves"] if s["key"] == "core")
    assert core["buy_eur"] == pytest.approx(400.0)
    # The locked satellite (6 % vs 0 %) is never sold, so it is not shown as out of band.
    nvda = next(r for r in out["lines"] if r["ticker"] == "NVDA")
    assert nvda["in_band"] is True and nvda["sell_eur"] == 0
    assert "tax_complete" in out


def test_sleeve_rebalance_keeps_an_unheld_unlocked_sleeve_visible(monkeypatch):
    from app.decision import monthly_plan
    from app.foundation.portfolio.metrics_wrappers import generate_rebalancing_suggestions

    db, user = _sleeve_book()
    plan = monthly_plan.sleeve_plan(db, user.id)
    plan["targets"] = {"core": 85.0, "tilt": 15.0, "satellite": 0.0}
    plan["unlocked"] = {"core": True, "tilt": True, "satellite": False}
    out = generate_rebalancing_suggestions(db, user.id, contribution_eur=1000.0, sleeve_plan=plan)
    row = next(s for s in out["suggestions"] if s["sleeve"] == "tilt")
    assert row["action"] == "buy" and row["estimated_amount"] > 0 and "no fund chosen" in row["ticker"]
    empty = generate_rebalancing_suggestions(_sleeve_book()[0], "nobody", sleeve_plan=plan)
    assert empty["estimate"] is True and empty["not_tax_advice"] is True
