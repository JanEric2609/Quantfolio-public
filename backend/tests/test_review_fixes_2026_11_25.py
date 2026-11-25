"""Fixes from the 2026-11-25 code review: two-sided confidence sequence, trial-dispersion DSR, paper ratio floor."""
import math

import numpy as np
import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_confidence_sequence_is_two_sided():
    from app.decision.graduation import ladder

    rng = np.random.default_rng(1)
    x = rng.normal(0.0, 0.01, 400)
    sigma = np.full(400, 0.01)
    cs = ladder.confidence_sequence(x, sigma)
    assert cs is not None
    t, v = 400, 400 * 0.01**2
    rho = 0.01**2 * ladder.TUITION_MIN_DAYS
    one_sided = math.sqrt((v + rho) * (2 * math.log(1 / ladder.CS_ALPHA) + math.log((v + rho) / rho)))
    radius = (cs["alpha_high"] - cs["alpha_low"]) / 2 / ladder.TRADING_DAYS_PER_YEAR * t
    assert radius > one_sided
    assert cs["level"] == 1 - ladder.CS_ALPHA


def test_gate_dsr_uses_trial_dispersion_not_below_the_null():
    from app.lab.evidence_gate import gate

    rng = np.random.default_rng(2)
    wide = pd.DataFrame({
        "a": rng.normal(0.02, 0.02, 120),
        "b": rng.normal(-0.01, 0.02, 120),
        "c": rng.normal(0.0, 0.02, 120),
    })
    var = gate._trial_sharpe_variance(wide)
    assert var is not None and var > 0
    spread = gate._candidate("a", wide["a"], True, 50, var).dsr
    default = gate._candidate("a", wide["a"], True, 50, None).dsr
    # Dispersed trials never lower the bar below the single-estimate null.
    assert spread <= default + 1e-12
    assert gate._trial_sharpe_variance(wide[["a"]]) is None


def test_paper_ratios_withheld_below_120_returns():
    from datetime import date, timedelta
    from decimal import Decimal

    from app.decision import paper_portfolio as pp
    from app.foundation.models.entities import PaperPortfolio, PaperSnapshot

    db = _memory_db()
    p = PaperPortfolio(user_id="u", name="t", initial_cash=Decimal("1000"))
    db.add(p)
    db.commit()
    start = date(2026, 1, 1)

    def seed(n):
        db.query(PaperSnapshot).delete()
        v = 1000.0
        for i in range(n):
            v *= 1.001 if i % 3 else 0.9985
            db.add(PaperSnapshot(portfolio_id=p.id, date=start + timedelta(days=i), total_value=Decimal(str(round(v, 4)))))
        db.commit()

    seed(60)
    short = pp.compute_metrics(db, p.id)
    assert short["updated"] and short["sharpe"] is None and short["max_drawdown"] is not None
    seed(pp.MIN_RETURNS_FOR_RATIOS + 2)
    assert pp.compute_metrics(db, p.id)["sharpe"] is not None


def test_advisor_diagnostics_snapshot_floor_follows_the_ratio_threshold():
    from app.decision import paper_portfolio as pp
    from app.decision.advisor import diagnostics

    assert diagnostics._MIN_METRIC_SNAPSHOTS == pp.MIN_RETURNS_FOR_RATIOS + 1
