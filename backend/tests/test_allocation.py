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
