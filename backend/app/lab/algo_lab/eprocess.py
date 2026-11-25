"""Funded promotion ledger: a cost-aware e-process per candidate (research prototype).

Follows the "Promote" protocol of Benhamou et al. (arXiv 2611.03384, ICAIF 2026):

* every candidate is born with a predictable share ``mass`` of one lifetime testing
  budget (the masses of all candidates ever registered sum to at most 1);
* evidence is a product of ``1 + lambda * y_t`` factors where ``y_t`` is the net-of-cost
  return divided by a predictable scale and clipped to [-1, 1], mixed over a fixed grid
  of bets (a convex mixture of supermartingales is a supermartingale);
* the candidate is promoted at the first time its evidence reaches ``1 / (alpha * mass)``;
  by Ville's inequality the chance that any candidate whose CLIPPED score has a
  non-positive conditional mean is ever promoted is at most ``alpha``, whatever the stopping
  rule or inspection frequency. The guarantee is about the clipped score ``Y``, not the
  unbounded net return (Benhamou et al., Sec. 2: "clipping is part of the estimand"). A
  zero-mean but negatively skewed return can have a POSITIVE clipped mean and then promotes
  with high probability, so ``scale`` must be large enough that clipping is rare
  (``clipped_fraction`` is reported; the paper saw 0.186 %);
* the crossing observation is never a deployed observation: exposure may start at the
  next one (``first_executable_index``);
* a rule change, restart or re-scale is a NEW candidate and spends new mass.

Scores must be computed on EXCESS returns over the declared benchmark when the claim
is "beats the benchmark" (Zheng 2026, arXiv 2610.07701: absolute-return criteria reject
every candidate, true ones included).

The ledger is in memory and JSON-serialisable (``to_dict``); persistence, hash chaining
and any scheduler belong to a later phase of ADR 0029. It is idempotent per
``event_key``: replaying an observation returns the stored evidence instead of
multiplying twice.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

BET_GRID: tuple[float, ...] = (0.025, 0.05, 0.10, 0.20, 0.35)
_EPS = 1e-12


class LedgerError(ValueError):
    """A registration or observation that would break the ledger's guarantees."""


def clipped_score(net_return: float, scale: float) -> float:
    """Bounded score ``clip(net_return / scale, -1, 1)``; ``scale`` must be positive."""
    if not scale > 0:
        raise LedgerError("scale must be positive")
    if not math.isfinite(net_return):
        raise LedgerError("net_return must be finite; pass an explicit 0.0 for an inactive day")
    return max(-1.0, min(1.0, net_return / scale))


@dataclass
class _Candidate:
    candidate_hash: str
    mass: float
    scale: float
    components: list[float] = field(default_factory=lambda: [1.0] * len(BET_GRID))
    n_obs: int = 0
    seen_keys: dict[str, float] = field(default_factory=dict)
    n_clipped: int = 0  # observations where |net_return / scale| > 1
    crossed_at: int | None = None  # 1-based index of the observation that crossed

    @property
    def evidence(self) -> float:
        return sum(self.components) / len(self.components)


class PromotionLedger:
    """Lifetime-budgeted promotion gate. ``alpha`` is the family-wise error bound."""

    def __init__(self, alpha: float = 0.05, budget: float = 1.0) -> None:
        if not 0 < alpha < 1:
            raise LedgerError("alpha must be in (0, 1)")
        if not 0 < budget <= 1:
            raise LedgerError("budget must be in (0, 1]")
        self.alpha = alpha
        self.budget = budget
        self._candidates: dict[str, _Candidate] = {}

    # -- registration -----------------------------------------------------
    @property
    def spent(self) -> float:
        return sum(c.mass for c in self._candidates.values())

    @property
    def remaining(self) -> float:
        return self.budget - self.spent

    def register(self, candidate_hash: str, mass: float, scale: float) -> None:
        """Fund a candidate. Fails closed: no duplicate hash, no overspend, no free restart."""
        if not candidate_hash:
            raise LedgerError("candidate_hash is required")
        if candidate_hash in self._candidates:
            raise LedgerError("candidate already registered; a restart is a new, funded candidate")
        if not mass > 0:
            raise LedgerError("mass must be positive")
        if mass > self.remaining + _EPS:
            raise LedgerError("lifetime testing budget exceeded")
        if not scale > 0:
            raise LedgerError("scale must be positive")
        self._candidates[candidate_hash] = _Candidate(candidate_hash, mass, scale)

    # -- evidence ---------------------------------------------------------
    def threshold(self, candidate_hash: str) -> float:
        return 1.0 / (self.alpha * self._get(candidate_hash).mass)

    def observe(self, candidate_hash: str, event_key: str, net_return: float) -> float:
        """Add one cost-adjusted observation; returns the mixture evidence after it.

        ``event_key`` (for example the trading date) makes replays idempotent.
        """
        cand = self._get(candidate_hash)
        if event_key in cand.seen_keys:
            return cand.seen_keys[event_key]
        y = clipped_score(net_return, cand.scale)
        if abs(net_return) > cand.scale:
            cand.n_clipped += 1
        cand.components = [e * (1.0 + lam * y) for e, lam in zip(cand.components, BET_GRID, strict=True)]
        cand.n_obs += 1
        evidence = cand.evidence
        cand.seen_keys[event_key] = evidence
        if cand.crossed_at is None and evidence >= 1.0 / (self.alpha * cand.mass):
            cand.crossed_at = cand.n_obs
        return evidence

    def evidence(self, candidate_hash: str) -> float:
        return self._get(candidate_hash).evidence

    def crossed(self, candidate_hash: str) -> bool:
        return self._get(candidate_hash).crossed_at is not None

    def first_executable_index(self, candidate_hash: str) -> int | None:
        """1-based index of the first observation that may be traded, or None if not crossed."""
        at = self._get(candidate_hash).crossed_at
        return None if at is None else at + 1

    def n_observations(self, candidate_hash: str) -> int:
        return self._get(candidate_hash).n_obs

    def to_dict(self) -> dict[str, object]:
        return {
            "alpha": self.alpha,
            "budget": self.budget,
            "spent": self.spent,
            "candidates": {
                h: {
                    "mass": c.mass,
                    "scale": c.scale,
                    "n_obs": c.n_obs,
                    "n_clipped": c.n_clipped,
                    "clipped_fraction": (c.n_clipped / c.n_obs) if c.n_obs else 0.0,
                    "evidence": c.evidence,
                    "threshold": 1.0 / (self.alpha * c.mass),
                    "crossed_at": c.crossed_at,
                }
                for h, c in self._candidates.items()
            },
        }

    def _get(self, candidate_hash: str) -> _Candidate:
        try:
            return self._candidates[candidate_hash]
        except KeyError as exc:
            raise LedgerError("unknown candidate") from exc
