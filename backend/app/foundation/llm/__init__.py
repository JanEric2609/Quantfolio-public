"""LLM services package: dual-backend dispatcher, priority queue scheduler, and audit logging."""

from .base import Completion, LLMBackend
from .router import call as router_call

__all__ = ["Completion", "LLMBackend", "router_call"]
