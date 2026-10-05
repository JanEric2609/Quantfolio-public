"""Contract-parity tests for the legacy Monte-Carlo projection retirement
(plan todo 28, ADR 0003 decision 2).

Pre-refactor response JSON for every migrated endpoint was captured into
``tests/fixtures/mc_parity/`` while ``quant.monte_carlo_projection`` still
existed. The retired function sampled from the global ``random`` stream; the
replacement (``quant_mc.projection.gbm_fan_projection``) draws from
``numpy.random.default_rng``, so terminal VALUES cannot match byte-for-byte
across engines by construction. The stable contract — and what these tests
assert — is the RESPONSE SHAPE: identical key sets at every nesting level,
identical value types, and identical array lengths (a deterministic function
of ``years``), plus monotonic percentile ordering. Zero keys were added or
removed, so no frontend type or consumer changed.

2026-10-04: the single-asset scenario endpoints and POST /monte-carlo were
removed with the old Monte Carlo tab; the goal endpoint now runs the wealth
planner (contributions, long-run real drift), and its fixture was recaptured
from that response (a superset of the old keys).
"""

import json
import random
from datetime import date, timedelta
from pathlib import Path

import pytest

from conftest import _memory_db

from app.foundation.models.entities import Goal, Holding, Portfolio, PriceCache, User

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mc_parity"


@pytest.fixture(autouse=True)
def _no_live_providers(monkeypatch):
    """Hermeticity guard: forbid any live provider fetch in this module.

    The endpoints under test must derive their price history solely from the
    seeded PriceCache rows via ``market.history``'s fresh-cache branch. Any
    fall-through to the provider registry (the yfinance rate-limit flake source
    under full-suite ``-n 6`` load) fails loudly here instead of hitting the
    network.
    """

    def _forbid(*_args, **_kwargs):
        raise AssertionError("Live provider fetch attempted in MC parity tests")

    monkeypatch.setattr("app.foundation.market.build_provider_registry", _forbid)


def _seeded_db():
    db = _memory_db()
    user = User(username="parity", password_hash="x", role="user")
    db.add(user)
    db.commit()
    portfolio = Portfolio(user_id=user.id, name="Main")
    db.add(portfolio)
    db.commit()
    db.add(
        Holding(
            portfolio_id=portfolio.id,
            name="World ETF",
            ticker="IWDA.AS",
            asset_type="etf",
            quantity=10,
            avg_buy_price=50,
            currency="EUR",
        )
    )
    # Seed inside market.history's 730-day lookback window so the fresh-cache
    # branch satisfies the endpoint without any live provider fetch (fetched_at
    # defaults to now_utc, passing the 24h HISTORY_TTL freshness gate).
    base_date = date.today() - timedelta(days=30)
    for i in range(30):
        db.add(
            PriceCache(
                ticker="IWDA.AS",
                date=base_date + timedelta(days=i),
                close=50.0 + i * 0.1 + (i % 7) * 0.05,
                source="yfinance",
            )
        )
    goal = Goal(
        user_id=user.id,
        title="Retirement",
        target_amount=100000,
        target_date=date.today() + timedelta(days=365 * 10),
        progress=5000,
        monthly_contribution=500,
    )
    db.add(goal)
    db.commit()
    return db, user, goal


def _shape(node, path="root"):
    """Reduce a JSON payload to {path: (sorted-keys | type, length)} entries."""
    if isinstance(node, dict):
        out = {path: (tuple(sorted(node.keys())), None)}
        for key, value in node.items():
            out.update(_shape(value, f"{path}.{key}"))
        return out
    if isinstance(node, list):
        out = {path: ("list", len(node))}
        if node:
            out.update(_shape(node[0], f"{path}[0]"))
        return out
    return {path: (type(node).__name__, None)}


def _assert_shape_matches(fixture_name, actual):
    expected = json.loads((FIXTURE_DIR / fixture_name).read_text())
    exp_shape, act_shape = _shape(expected), _shape(actual)
    assert set(exp_shape) == set(act_shape), (
        f"{fixture_name}: payload paths diverged. "
        f"missing={set(exp_shape) - set(act_shape)} extra={set(act_shape) - set(exp_shape)}"
    )
    for path in exp_shape:
        assert act_shape[path] == exp_shape[path], (
            f"{fixture_name}:{path}: expected {exp_shape[path]}, got {act_shape[path]}"
        )


def _assert_percentiles_monotonic(projection):
    assert 0 < projection["p05_terminal"] <= projection["median_terminal"] <= projection["p95_terminal"]
    fan = projection["fan_data"]
    for i in range(fan["steps"]):
        band = fan["bands"][i]
        assert band["p05"] <= band["p25"] <= band["median"] <= band["p75"] <= band["p95"]


def test_goal_mc_endpoint_response_shape_stable():
    from app.interface.api.quant.goals import goal_monte_carlo

    db, user, goal = _seeded_db()
    random.seed(45)
    resp = goal_monte_carlo(goal_id=goal.id, db=db, user=user)

    assert resp["status"] == "completed"
    _assert_shape_matches("goal_mc.json", resp)
    assert len(resp["fan_chart"]) == len(resp["median_path"]) > 0


def test_projection_is_seed_reproducible():
    """The replacement engine can do something the legacy one could not:
    reproduce a run exactly from a seed."""
    from app.foundation.quant_mc.projection import gbm_fan_projection

    a = gbm_fan_projection(10000, 0.07, 0.15, years=2, simulations=200, seed=7)
    b = gbm_fan_projection(10000, 0.07, 0.15, years=2, simulations=200, seed=7)
    assert a == b
