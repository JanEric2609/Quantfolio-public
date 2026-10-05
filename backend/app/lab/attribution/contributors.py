"""Top contributors/detractors extraction from Brinson results."""

from dataclasses import dataclass
from app.lab.attribution.brinson import SecurityAttribution


@dataclass
class Contributor:
    """Top contributor or detractor."""
    isin: str
    ticker: str
    name: str
    total_contribution: float
    allocation_effect: float
    selection_effect: float
    interaction_effect: float
    portfolio_weight: float
    benchmark_weight: float


def top_contributors(
    securities: list[SecurityAttribution],
    n: int = 10,
    direction: str = "positive",
) -> list[Contributor]:
    """
    Extract top N contributors or detractors.

    Args:
        securities: List of SecurityAttribution from Brinson decomposition
        n: Number of top contributors to return
        direction: "positive" (gainers), "negative" (detractors), or "both"

    Returns:
        List of top Contributor objects sorted by contribution magnitude
    """
    contributors = []
    for sec in securities:
        contrib = Contributor(
            isin=sec.isin,
            ticker=sec.ticker,
            name=sec.name,
            total_contribution=sec.total_effect,
            allocation_effect=sec.allocation_effect,
            selection_effect=sec.selection_effect,
            interaction_effect=sec.interaction_effect,
            portfolio_weight=sec.portfolio_weight,
            benchmark_weight=sec.benchmark_weight,
        )
        contributors.append(contrib)

    # Filter by direction
    if direction == "positive":
        contributors = [c for c in contributors if c.total_contribution > 0]
        contributors.sort(key=lambda c: c.total_contribution, reverse=True)
    elif direction == "negative":
        contributors = [c for c in contributors if c.total_contribution < 0]
        contributors.sort(key=lambda c: c.total_contribution)  # Most negative first
    elif direction == "both":
        contributors.sort(key=lambda c: abs(c.total_contribution), reverse=True)

    return contributors[:n]
