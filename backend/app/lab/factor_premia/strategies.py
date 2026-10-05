"""The pre-registered strategy set and the rules a strategy must pass.

Everything here is fixed *before* any result is seen (report §6, "prior-
informed test"). Adding a strategy or loosening a rule after looking at the
numbers turns the test into a search; each strategy here is also written to
the global trial ledger, so the search breadth is counted either way.
"""
from __future__ import annotations

from dataclasses import dataclass

# MSCI Europe's 15 developed markets, as JKP ``excntry`` (ISO 3166 alpha-3).
EUROPE_DEVELOPED: tuple[str, ...] = (
    "AUT", "BEL", "CHE", "DEU", "DNK", "ESP", "FIN", "FRA",
    "GBR", "IRL", "ITA", "NLD", "NOR", "PRT", "SWE",
)
# MSCI World's 23 developed markets: the universe of the core ETF, and of the
# world factor ETFs a tilt would be held in. This is the region that gates the
# tilt (``TILT_EVIDENCE_REGION``); Europe is kept as a second, informational
# test. Before a country's JKP coverage starts it simply has no rows, so the
# early years are US only.
WORLD_DEVELOPED: tuple[str, ...] = (
    "AUS", "CAN", "HKG", "ISR", "JPN", "NZL", "SGP", "USA", *EUROPE_DEVELOPED,
)
REGIONS: dict[str, tuple[str, ...]] = {"world": WORLD_DEVELOPED, "europe": EUROPE_DEVELOPED}


@dataclass(frozen=True)
class Strategy:
    key: str
    label: str
    # SQL expression over JKP columns; a higher value is a better stock.
    signal: str
    # First publication, for the post-publication check (McLean & Pontiff 2016).
    published: int
    citation: str
    # What a long-only UCITS implementation of the tilt would be.
    etf_hint: str


STRATEGIES: tuple[Strategy, ...] = (
    Strategy(
        "value", "Value", "be_me", 1992,
        "Fama & French (1992); Rosenberg, Reid & Lanstein (1985)",
        "a world value factor ETF",
    ),
    Strategy(
        "momentum", "Momentum", "mom_12_1", 1993,
        "Jegadeesh & Titman (1993)",
        "a world momentum factor ETF",
    ),
    Strategy(
        "profitability", "Profitability", "gp_at", 2013,
        "Novy-Marx (2013)",
        "a world quality factor ETF",
    ),
    Strategy(
        "investment", "Low investment", "-at_gr1", 2008,
        "Cooper, Gulen & Schill (2008)",
        "no dedicated UCITS ETF; part of multi-factor funds",
    ),
    # Scored as the average of the value and momentum ranks (see portfolios).
    Strategy(
        "value_momentum", "Value + momentum", "", 2013,
        "Asness, Moskowitz & Pedersen (2013)",
        "a value ETF and a momentum ETF, half each",
    ),
)
STRATEGY_BY_KEY = {s.key: s for s in STRATEGIES}

# --- the test ------------------------------------------------------------------

MIN_MONTHS = 240             # 20 years of monthly cross-sections
MIN_T_STAT = 2.0             # documented premium: prior-informed, not t > 3
MIN_POST_PUBLICATION_MONTHS = 60
NEWEY_WEST_LAGS = 6
# McLean & Pontiff (2016): returns fall ~58 % after publication. Applied to the
# whole-sample mean, so it also discounts the post-publication part; that
# double-counting is deliberate and only ever makes the estimate smaller.
PUBLICATION_DECAY = 0.58
# A factor ETF costs more than the world ETF it sits next to (TER gap plus
# in-fund trading on each rebalance), per year.
IMPLEMENTATION_COST = 0.004
# Abgeltungsteuer + Soli on an equity fund with 30 % Teilfreistellung. The
# tilt and the core are taxed alike, so tax scales the excess down by this.
AFTER_TAX_FACTOR = 1.0 - 0.26375 * (1.0 - 0.30)
# Book-level window for the "how far behind can this leave me" figure.
LAG_WINDOW_MONTHS = 60
# Portfolio construction.
MIN_STOCKS_PER_COUNTRY_MONTH = 15
EXCLUDED_SIZE_GROUPS: tuple[str, ...] = ("micro", "nano")
