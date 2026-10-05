"""Discover context (bounded): forward-looking candidate discovery.

Owns the 8-stage candidate pipeline (pipeline), headless macro regime gate
(regime_gate), provider-degradation scoring, LLM dossier generation
(dossier_writer), the forward-prediction ledger (ledger), EU tradeability
checks (tradeability), config management + perturbation + review
(config/config_perturb/config_review), and the background job orchestrator
(orchestrator/jobs).

This ``__init__`` is the context facade: it re-exports the callables other
contexts consume today. Import the package root, not internal submodules.
"""

from app.decision.discover.regime_gate import MacroRegimeGate, RegimeGateResult
from app.decision.discover.provider_degradation import DegradationAssessor, DegradationResult
from app.decision.discover.config import (
    DEFAULT_SIGNAL_WEIGHTS,
    DEFAULT_PROMPT_TEMPLATE,
    get_active_config,
    get_or_seed_active_config,
    get_champion_config,
    get_config,
    is_config_stale,
    list_configs,
    create_config,
    activate_config,
    sync_config_to_default,
)
from app.decision.discover.config_perturb import (
    generate_weight_perturbations,
    generate_prompt_perturbations,
)
from app.decision.discover.framings import STYLE_FRAMINGS
from app.decision.discover.ledger import DEFAULT_HORIZON_DAYS
from app.decision.discover.calibrator import calibrate_prediction
from app.decision.discover.predictor import store_prediction
from app.decision.discover.skill_snapshot import skill_summary
from app.decision.discover.state import DiscoveryState
from app.decision.discover.tradeability import assess as assess_tradeability
from app.decision.discover.pipeline import run_candidate_pipeline
from app.decision.discover.dossier_writer import deterministic_template, write_dossier
from app.decision.discover.orchestrator import (
    reap_run_if_stale,
    reap_stale_discover_runs,
    submit_discover_job,
)
from app.decision.discover.jobs import (
    register_discover_refresh_job,
    register_discovery_resolution_job,
    register_discovery_review_job,
)

__all__ = [
    "skill_summary",
    "DEFAULT_HORIZON_DAYS",
    "DEFAULT_PROMPT_TEMPLATE",
    "DEFAULT_SIGNAL_WEIGHTS",
    "DiscoveryState",
    "DegradationAssessor",
    "DegradationResult",
    "MacroRegimeGate",
    "RegimeGateResult",
    "STYLE_FRAMINGS",
    "activate_config",
    "assess_tradeability",
    "calibrate_prediction",
    "create_config",
    "deterministic_template",
    "generate_prompt_perturbations",
    "generate_weight_perturbations",
    "get_active_config",
    "get_champion_config",
    "get_config",
    "get_or_seed_active_config",
    "is_config_stale",
    "list_configs",
    "reap_run_if_stale",
    "reap_stale_discover_runs",
    "register_discover_refresh_job",
    "register_discovery_resolution_job",
    "register_discovery_review_job",
    "run_candidate_pipeline",
    "store_prediction",
    "submit_discover_job",
    "sync_config_to_default",
    "write_dossier",
]
