"""What the long-history test of the plan's asset mix found (ADR 0022, run once on 2026-10-14).

The plan's core is 100 % global equity (rule R0). ADR 0022 tested five other
mixes on 16 countries since 1870 (JST Macrohistory R.6, block bootstrap, a
monthly saver over 30 years, German tax, CRRA gamma 5). None passed, so none is
recommended. A bond line the owner sets anyway carries its tested cost here,
as the ADR's "What the recommender shows" asks: a history, not a forecast.

The figures are copied from the ADR's "Results" section (code 6466a697). They
change only with a new, pre-registered run.
"""
from __future__ import annotations

from dataclasses import dataclass

SOURCE = "ADR 0022: 16 countries, 1870-2020, a monthly saver over 30 years"


@dataclass(frozen=True)
class TestedMix:
    """One ADR 0022 rule at 30 years, gamma 5, primary costs."""

    rule: str
    label: str
    #: ADR 0022's Δ = CE_rule / CE_R0 - 1, pooled over the 16 countries: a 100 %-equity saver
    #: needs Δ more savings to match the rule (Δ < 0: |Δ| less).
    pooled_delta: float
    #: The median of the 16 per-country changes.
    median_country_delta: float
    #: Median maximum drawdown of the plan's wealth.
    max_dd_median: float
    passed: bool = False


R0_MAX_DD_MEDIAN = 0.48
BOND_MIXES: tuple[TestedMix, ...] = (
    TestedMix("R2", "a 20 % bond sleeve", -0.054, 0.029, 0.39),
    TestedMix("R3", "a 40 % bond sleeve", -0.142, 0.007, 0.31),
)

MIX_NOTE = (
    "Why 100 % global equity: on 16 countries since 1870, no other mix tested beat it for a monthly "
    "saver over 30 years: home bias, 20 % or 40 % bonds, a glide path into bonds, or a cash sleeve for a "
    "planned withdrawal (ADR 0022). Bonds lowered the typical worst fall (31-39 % instead of 48 %) but "
    "cost saving power where they were inflated away (Japan, Finland, Portugal, Germany before 1950). "
    "A history, not a forecast."
)


def _signed_pct(x: float) -> str:
    return f"{abs(100.0 * x):.1f} %"


def bond_mix(target_pct: float) -> TestedMix:
    """The tested mix nearest to a bond target: up to 30 % reads as 20 %, above as 40 %."""
    return BOND_MIXES[0] if target_pct <= 30.0 else BOND_MIXES[1]


def bond_cost_note(target_pct: float) -> str:
    """The cost line shown next to the owner's bond target. It describes history and forecasts nothing.

    Told from the 100 %-equity saver's side, which is how ADR 0022 defines Δ,
    so the figures are the ADR's own without conversion.
    """
    mix = bond_mix(target_pct)
    pooled = "more" if mix.pooled_delta > 0 else "less"
    median = "more" if mix.median_country_delta > 0 else "less"
    return (
        f"Tested ({SOURCE}): {mix.label} did not beat 100 % equity. Across all 16 countries, 100 % "
        f"equity reached the same outcome with about {_signed_pct(mix.pooled_delta)} {pooled} savings; "
        f"in the median country it needed about {_signed_pct(mix.median_country_delta)} {median}. "
        f"Typical worst fall "
        f"{100 * mix.max_dd_median:.0f} % against {100 * R0_MAX_DD_MEDIAN:.0f} %. Your target, your "
        f"choice; a history, not a forecast."
    )
