"""Headless macro regime gate for the discovery pipeline.

The gate answers two questions from the latest persisted regime snapshot:
1. Should discovery be paused entirely? (crisis / bear regimes)
2. How favourable is the current regime for new-asset discovery?

Regime source: the jump model's latest ``regime_snapshots`` row (source
"jump"), the same state the header chip shows. Without one the label is
"unknown" (neutral), never a rule-based guess.

It is read-only, synchronous, and has no FastAPI/HTTP dependencies.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.data_backbone.regime_store import RegimeStore
from app.lab.regime.gate import (
    CRISIS_VIX_THRESHOLD,
    RegimeContext,
    RegimeGate,
    get_regime_label,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegimeGateResult:
    """Outcome of a macro-regime gate check.

    Attributes:
        regime_label: Normalised regime label (e.g. ``bull``, ``bear``).
        risk_on: Whether the current regime is considered risk-on for discovery.
        crisis: Whether crisis conditions are active.
        confidence: Confidence of the regime snapshot, or ``0.5`` if unavailable.
        allowed: Whether discovery may proceed.
        reason: Human-readable block reason when ``allowed`` is ``False``.
    """

    regime_label: str
    risk_on: bool
    crisis: bool
    confidence: float
    allowed: bool
    reason: str | None = None


class MacroRegimeGate:
    """Coarse macro gate for the Discover pipeline.

    Usage::

        gate = MacroRegimeGate(db)
        if gate.blocks_discovery():
            return []
        alignment = gate.alignment_score()
        ctx = gate.context_dict()
    """

    def __init__(self, db: Session | None = None):
        self._db = db
        self._gate = RegimeGate(db) if db is not None else None
        self._store = RegimeStore(db) if db is not None else None
        self._block_reason: str | None = None
        # Which model the last _latest_result read: the regime_snapshots
        # row's source ("jump", or "hmm") or "rule_based" (the header chip's
        # VIX/curve/momentum rule).
        self._source: str | None = None

    @classmethod
    def block_all(cls, reason: str) -> "MacroRegimeGate":
        """Factory that creates a gate which always blocks discovery."""
        gate = cls.__new__(cls)
        gate._db = None
        gate._gate = None
        gate._store = None
        gate._block_reason = reason
        gate._source = None
        return gate

    def _ensure_session(self, db: Session | None = None) -> Session:
        session = db or self._db
        if session is None:
            raise ValueError("MacroRegimeGate requires a SQLAlchemy Session")
        return session

    def _latest_result(self, db: Session | None = None) -> RegimeGateResult:
        """Assess the latest regime snapshot without an asset-specific filter."""
        if self._block_reason is not None:
            return RegimeGateResult(
                regime_label="blocked",
                risk_on=False,
                crisis=False,
                confidence=0.0,
                allowed=False,
                reason=self._block_reason,
            )

        session = self._ensure_session(db)
        store = self._store if self._store is not None else RegimeStore(session)
        # The jump model is the one regime model (2026-10); HMM fallback rows are ignored.
        snapshot = store.get_latest_snapshot(source="jump")

        if snapshot is None:
            # No jump-model row yet: the same reader the header chip uses,
            # which reports "unknown" rather than guess from a rule.
            from app.lab.regime.gate import CRISIS_VIX_THRESHOLD
            from app.lab.regime.macro_snapshot import get_or_refresh_regime

            self._source = "jump"
            fallback = get_or_refresh_regime(session)
            label = fallback.get("label", "unknown") if isinstance(fallback, dict) else "unknown"
            confidence = fallback.get("confidence", 0.5) if isinstance(fallback, dict) else 0.5
            vix = fallback.get("vix") if isinstance(fallback, dict) else None
            crisis = label == "bear" or (isinstance(vix, (int, float)) and vix > CRISIS_VIX_THRESHOLD)
            logger.info("No jump-model regime_snapshots row; regime %r", label)
            return RegimeGateResult(
                regime_label=label,
                risk_on=(label == "bull"),
                crisis=crisis,
                confidence=float(confidence) if isinstance(confidence, (int, float)) else 0.5,
                allowed=not crisis,
                reason="crisis regime" if crisis else None,
            )

        self._source = str(snapshot.get("source") or "jump")
        ctx = self._build_context(snapshot)
        label = ctx.label
        crisis = ctx.crisis
        confidence = ctx.confidence if ctx.confidence is not None else 0.5

        # Block discovery only in crisis or bear regimes. Other regimes are
        # allowed so the composite scorer can express a continuous alignment.
        if crisis or label == "bear":
            return RegimeGateResult(
                regime_label=label,
                risk_on=False,
                crisis=True,
                confidence=confidence,
                allowed=False,
                reason=f"Discovery blocked: {'crisis' if crisis else 'bear'} regime ({label})",
            )

        risk_on = label in {"bull"}
        return RegimeGateResult(
            regime_label=label,
            risk_on=risk_on,
            crisis=False,
            confidence=confidence,
            allowed=True,
            reason=None,
        )

    def blocks_discovery(self) -> bool:
        """Return True when new discovery should be paused."""
        return not self._latest_result().allowed

    def alignment_score(self) -> float:
        """Return a 0-1 score for how favourable the current regime is."""
        result = self._latest_result()
        if not result.allowed:
            return 0.0
        if result.risk_on:
            return 1.0
        # Non-bull but allowed regimes receive a cautious positive score.
        alignment_map = {"sideways": 0.7, "unknown": 0.6}
        return alignment_map.get(result.regime_label, 0.6)

    def context_dict(self) -> dict[str, Any]:
        """Return the full regime context as a JSON-safe dict."""
        result = self._latest_result()
        return {
            "regime_label": result.regime_label,
            "risk_on": result.risk_on,
            "crisis": result.crisis,
            "confidence": result.confidence,
            "allowed": result.allowed,
            "reason": result.reason,
            # The dossier and the header chip can show different regimes;
            # this says which model the dossier's came from.
            "source": self._source,
        }

    def assess(self, asset: str, db: Session | None = None) -> RegimeGateResult:
        """Asset-aware assessment. Currently delegates to the global snapshot."""
        result = self._latest_result(db)
        if result.reason and asset:
            reason = result.reason.replace("Discovery blocked", f"Discovery blocked for {asset}")
            return RegimeGateResult(
                regime_label=result.regime_label,
                risk_on=result.risk_on,
                crisis=result.crisis,
                confidence=result.confidence,
                allowed=result.allowed,
                reason=reason,
            )
        return result

    def _build_context(self, snapshot: dict[str, Any]) -> RegimeContext:
        label = get_regime_label(snapshot)
        crisis = self._is_crisis(snapshot, label)
        confidence = self._confidence(snapshot)
        return RegimeContext(
            label=label,
            confidence=confidence,
            crisis=crisis,
        )

    @staticmethod
    def _is_crisis(snapshot: dict[str, Any], label: str) -> bool:
        if label == "bear":
            return True
        vix = snapshot.get("vix")
        if not isinstance(vix, (int, float)):
            payload = snapshot.get("payload") or {}
            vix = payload.get("vix")
        return isinstance(vix, (int, float)) and vix > CRISIS_VIX_THRESHOLD

    @staticmethod
    def _confidence(snapshot: dict[str, Any]) -> float | None:
        score = snapshot.get("score")
        if isinstance(score, (int, float)):
            return float(score)
        return None
