"""The guarded derivatives desk (ADR 0027): paper only, no order path.

Public surface (other contexts import this package root, never a submodule):
the limits, the ledger, the lifecycle and scorecard reads, the job registrars
and the Telegram registration.
"""
from .api_ops import (
    DeskActionError, chain_status, close_position_api, desk_status, list_events, list_positions, submit_idea,
    take_proposal,
)
from .jobs import register_desk_monitor_job, register_desk_reconcile_job
from .ledger import append_event, verify_chain
from .limits import LIMITS
from .prereg import PREREG_HASH, register as register_desk
from .scoring import scorecard
from .service import is_halted, halt_info, set_halt, health, owner_close, propose
from .telegram import register_desk_telegram

__all__ = [
    "DeskActionError", "chain_status", "close_position_api", "desk_status", "list_events", "list_positions",
    "submit_idea", "take_proposal", "LIMITS", "PREREG_HASH", "append_event", "halt_info", "health", "is_halted", "owner_close", "propose",
    "register_desk", "register_desk_monitor_job", "register_desk_reconcile_job", "register_desk_telegram",
    "scorecard", "set_halt", "verify_chain",
]
