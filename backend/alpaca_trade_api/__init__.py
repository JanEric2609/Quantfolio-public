"""Vendored stub — this is NOT the real ``alpaca-trade-api`` PyPI package.

``finrl/__init__.py`` unconditionally does ``from finrl.trade import trade``,
which pulls in ``finrl.meta.env_stock_trading.env_stock_papertrading`` and
needs ``alpaca_trade_api.REST`` to exist purely so the import chain resolves.
The only real usage is inside ``AlpacaPaperTrading.__init__`` — a class this
app never instantiates: live/paper trading via a broker API is explicitly
prohibited here (see AGENTS.md's safety constraints). Everything this app's
``quant_rl`` actually uses from finrl (``YahooDownloader``,
``FeatureEngineer``, ``StockPortfolioEnv``, ``StockTradingEnv``) lives in
``finrl.meta.*`` and has nothing to do with Alpaca.

The real PyPI package cannot be installed here. Its latest (and final —
the project is dead upstream) release, 3.2.0, hard-pins ``websockets<11``
and ``urllib3<2``. This app's ``yfinance`` dependency (core market-data
provider, imported everywhere) requires ``websockets>=13`` — its own
``__init__.py`` does ``from .live import WebSocket, AsyncWebSocket``, which
needs ``websockets.sync.client``, a module that does not exist before
websockets 11. Installing the real ``alpaca-trade-api`` package breaks
``import yfinance`` app-wide — confirmed directly, not a theoretical
resolver warning. See AGENTS.md's "Dependency watch-list" for the summary.

If a future change genuinely needs live/paper trading via Alpaca, that is
a deliberate, isolated integration decision — not something that should
silently reappear here as a version bump.
"""
from __future__ import annotations

from typing import Any


class REST:
    """Stub — raises if anything actually tries to use it.

    Never called in production: ``AlpacaPaperTrading`` (finrl's only
    consumer of this symbol) is never instantiated by this app.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "alpaca_trade_api is vendored as a stub in this project — "
            "live/paper trading via Alpaca is not supported. "
            "See backend/alpaca_trade_api/__init__.py."
        )
