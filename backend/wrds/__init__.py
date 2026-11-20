"""Vendored stub — this is NOT the real ``wrds`` (Wharton Research Data
Services) PyPI package.

``finrl/__init__.py`` unconditionally does ``from finrl.train import
train``, which pulls in ``finrl.meta.data_processor.DataProcessor`` ->
``finrl.meta.data_processors.processor_wrds.WrdsProcessor``, needing
``wrds.Connection`` to exist purely so the import chain resolves. This app
never uses WRDS as a data source (not part of the provider chain — see
``app/services/providers/``) and never instantiates ``WrdsProcessor``.

The real PyPI package cannot be installed here: it pins ``pandas<2.3``,
which would downgrade this app's pandas (a core dependency used
everywhere in the quant/ML stack) from 2.3.x — a high-risk, unjustified
downgrade for a data vendor this app doesn't use. Same category of
problem as ``alpaca_trade_api`` (see that stub's docstring) — see
AGENTS.md's "Dependency watch-list" for the summary.
"""
from __future__ import annotations

from typing import Any


class Connection:
    """Stub — raises if anything actually tries to use it.

    Never called in production: ``WrdsProcessor`` (finrl's only consumer
    of this symbol) is never instantiated by this app.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "wrds is vendored as a stub in this project — WRDS is not "
            "used as a data source. See backend/wrds/__init__.py."
        )
