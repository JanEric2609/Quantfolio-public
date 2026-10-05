"""What-if scenarios for Portfolio -> Risk.

Each scenario applies a fixed shock to the holdings as they are today. They are
illustrations of exposure, not forecasts: no probabilities are attached (the old
page printed invented "1-in-20-year" frequencies), and there is no Monte Carlo
number here (the old "Monte Carlo tail risk" card was a historical VaR on
snapshot ``total_value``, which counts salary and purchases as return).

The scenarios kept are the ones whose impact is driven by something measured:

* two FX scenarios, whose effect follows the currencies each holding actually
  moves with (priced-in currency, and the index's constituents for a mapped
  index ETF, ``foundation.etf_currency``);
* one uniform market drop, scaled by asset type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.foundation.etf_currency import exposure_for_holding
from app.foundation.models.entities import Holding, Portfolio
from app.foundation.portfolio_utils import holding_market_value


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class StressScenario:
    """Defines a single what-if scenario with market/asset-type/currency shocks."""

    name: str
    description: str
    market_shock_pct: float = 0.0  # broad market shock (negative = down)
    sector_shocks: dict[str, float] = field(default_factory=dict)  # asset_type → shock %
    currency_shock_pct: float = 0.0  # change of the base currency vs foreign currencies


@dataclass
class StressResult:
    """Result of applying a single scenario to a portfolio."""

    scenario_name: str
    description: str
    portfolio_impact_pct: float  # overall portfolio value change
    worst_case_loss: float  # largest single-holding loss in EUR
    details: dict[str, float] = field(default_factory=dict)  # per-holding impacts


@dataclass
class StressTestResult:
    """Aggregated result across all scenarios."""

    portfolio_id: str
    scenarios: list[StressResult] = field(default_factory=list)
    note: str = (
        "What-if scenarios on today's holdings, not forecasts. Impacts use fixed shocks "
        "by asset type and the currencies your holdings actually move with."
    )
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ---------------------------------------------------------------------------
# Predefined scenarios
# ---------------------------------------------------------------------------

PREDEFINED_SCENARIOS: list[StressScenario] = [
    StressScenario(
        name="Market Crash",
        description=(
            "Broad market decline of 30% with all asset types impacted. "
            "Simulates a severe recession or financial crisis."
        ),
        market_shock_pct=-0.30,
        sector_shocks={
            "stock": -0.30,
            "etf": -0.28,
            "bond": -0.05,
            "crypto": -0.50,
            "commodity": -0.20,
            "reit": -0.35,
        },
    ),
    StressScenario(
        name="Currency Crisis",
        description=(
            "EUR depreciates 15% vs USD while European assets sell off. "
            "Holdings priced in foreign currencies gain in euro terms and "
            "cushion the fall."
        ),
        market_shock_pct=-0.05,
        currency_shock_pct=-0.15,
        sector_shocks={
            "stock": -0.08,
            "etf": -0.06,
            "bond": -0.03,
            "crypto": -0.10,
            "commodity": 0.05,
            "reit": -0.07,
        },
    ),
    # The FX risk of a euro investor in global equities is the dollar
    # falling, which "Currency Crisis" (the euro falling) cannot show: there
    # foreign holdings gain. EUR/USD rose from about 1.03 to 1.17 in the
    # first half of 2025. Only the currency moves here, so the loss is the
    # portfolio's foreign-priced share.
    StressScenario(
        name="Dollar Slump",
        description=(
            "EUR appreciates 15% vs USD with local markets flat. Holdings "
            "priced in foreign currencies lose value in euro terms."
        ),
        currency_shock_pct=0.15,
    ),
]


# ---------------------------------------------------------------------------
# Holding shock helpers
# ---------------------------------------------------------------------------

def _apply_scenario_to_holding(
    holding: Holding,
    scenario: StressScenario,
    base_currency: str,
    db: Session | None = None,
) -> float:
    """Return the shock fraction for a single holding under *scenario*."""
    asset_type = holding.asset_type.lower()

    # Start with the asset-type shock
    shock = scenario.sector_shocks.get(asset_type, scenario.market_shock_pct)

    # Currency shock on the share of the holding that is not in the base
    # currency: when the base currency depreciates, that share GAINS (negate
    # the shock); when it appreciates, it loses. The share comes from the
    # priced-in currency, not the booking currency (DKB books a Nasdaq share
    # in EUR), and for a mapped index ETF from its index's constituents:
    # EUNL.DE is quoted in EUR, but ~92% of MSCI World trades in other
    # currencies, so a Xetra ETF used to escape every FX shock.
    if scenario.currency_shock_pct != 0:
        shock -= scenario.currency_shock_pct * exposure_for_holding(db, holding).foreign_share(base_currency)

    return shock


# ---------------------------------------------------------------------------
# Core: run stress test
# ---------------------------------------------------------------------------

def run_stress_test(portfolio_id: str, db: Session) -> StressTestResult:
    """Apply every predefined scenario to the current holdings of *portfolio_id*."""
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        return StressTestResult(portfolio_id=portfolio_id)

    base_currency = portfolio.currency or "EUR"

    holdings: list[Holding] = (
        db.query(Holding)
        .filter(Holding.portfolio_id == portfolio_id)
        .all()
    )
    if not holdings:
        return StressTestResult(portfolio_id=portfolio_id)

    total_value = 0.0
    holding_values: list[tuple[Holding, float]] = []
    for h in holdings:
        val = holding_market_value(db, h)
        total_value += val
        holding_values.append((h, val))

    if total_value <= 0:
        return StressTestResult(portfolio_id=portfolio_id)

    scenario_results: list[StressResult] = []
    for scenario in PREDEFINED_SCENARIOS:
        portfolio_impact = 0.0
        worst_loss = 0.0
        details: dict[str, float] = {}

        for holding, value in holding_values:
            shock = _apply_scenario_to_holding(holding, scenario, base_currency, db)
            portfolio_impact += shock * (value / total_value)

            # A holding that gains under the scenario loses nothing; abs()
            # reported a 7% gain as a EUR 119 "worst-case loss".
            worst_loss = max(worst_loss, max(0.0, -shock) * value)
            details[holding.ticker or holding.name] = round(shock, 4)

        scenario_results.append(
            StressResult(
                scenario_name=scenario.name,
                description=scenario.description,
                portfolio_impact_pct=round(portfolio_impact * 100, 2),
                worst_case_loss=round(worst_loss, 2),
                details=details,
            )
        )

    return StressTestResult(portfolio_id=portfolio_id, scenarios=scenario_results)
