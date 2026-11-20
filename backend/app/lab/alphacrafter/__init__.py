"""AlphaCrafter context (bounded): factor-mining agent pipeline.

Three-agent system (miner / screener / trader) for quantitative factor
research and portfolio optimisation, with dossier generation, orchestrator,
tuning jobs, IC-decay factor retirement, and an LLM factor proposer.

This ``__init__`` is the context facade: it re-exports the callables other
contexts consume today. Import the package root, not internal submodules.

Not re-exported here, by cycle constraint: ``jobs.register_alphacrafter_daily_job``
and ``jobs.register_alphacrafter_tuning_job`` — jobs.py itself imports this
package root (``from app.lab.alphacrafter import run_daily_alphacrafter``),
so a root -> jobs edge would close a module-level import cycle. Consumers
(app.worker) import ``app.lab.alphacrafter.jobs`` directly.
"""

from app.lab.alphacrafter import orchestrator
from app.lab.alphacrafter.dossier import build_dossier, compute_conviction
from app.lab.alphacrafter.miner import run_miner
from app.lab.alphacrafter.orchestrator import (
    reap_stale_job_runs,
    run_daily_alphacrafter,
)
from app.lab.alphacrafter.screener import run_screener
from app.lab.alphacrafter.trader import run_trader

__all__ = [
    "build_dossier",
    "compute_conviction",
    "orchestrator",
    "reap_stale_job_runs",
    "run_daily_alphacrafter",
    "run_miner",
    "run_screener",
    "run_trader",
]
