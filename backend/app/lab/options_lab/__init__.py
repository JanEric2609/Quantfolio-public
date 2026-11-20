"""Options research (ADR 0026): what a protective put costs, and what an issuer charges on a warrant.

Neither feeds a recommendation. Discover and the advisor do not suggest
options; this package measures whether they ever should.
"""
from app.lab.options_lab.protective_put import protective_put_backtest
from app.lab.options_lab.warrant_margin import warrant_margin, warrant_margin_from_db

__all__ = ["protective_put_backtest", "warrant_margin", "warrant_margin_from_db"]
