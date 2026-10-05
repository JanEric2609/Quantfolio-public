"""Shared Memory H — central coordination state for the AlphaCrafter pipeline.

Implements the paper's Shared Memory H architecture: a single dataclass
that all agents (Miner, Screener, Trader) read from and write to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


@dataclass
class MarketState:
    """Current market universe and macro environment."""

    universe: list[str]
    date_range: tuple[str, str]
    regime_label: str | None
    crisis: bool
    macro_indicators: dict[str, float]
    data_availability: dict[str, int]


@dataclass
class FactorMetrics:
    """Statistical quality metrics for a factor."""

    ic: float
    icir: float
    turnover: float
    decay_halflife_days: float | None
    n_obs: int


@dataclass
class FactorState:
    """Full state of a single factor in the pipeline."""

    id: str
    name: str
    source: str  # seed | llm | user
    category: str
    formula: str
    dsl: str | None
    metrics: FactorMetrics
    regime_applicability: dict[str, float]


@dataclass
class SharedMemoryH:
    """Central coordination state shared across all AlphaCrafter agents.

    This is the 'H' (holographic memory) from the Shared Memory H architecture.
    """

    market_state: MarketState
    factor_states: list[FactorState] = field(default_factory=list)
    regime_assessment: dict = field(default_factory=dict)
    miner_outputs: dict = field(default_factory=dict)
    screener_outputs: dict = field(default_factory=dict)
    trader_outputs: dict = field(default_factory=dict)
    evaluation: dict = field(default_factory=dict)
    agent_history: list[dict] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def append_history(self, agent: str, action: str, details: dict) -> None:
        """Add a timestamped action to the agent history log."""
        self.agent_history.append(
            {
                "agent": agent,
                "action": action,
                "details": details,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
        self.updated_at = datetime.now(UTC)

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dict."""
        return {
            "market_state": {
                "universe": self.market_state.universe,
                "date_range": list(self.market_state.date_range),
                "regime_label": self.market_state.regime_label,
                "crisis": self.market_state.crisis,
                "macro_indicators": self.market_state.macro_indicators,
                "data_availability": self.market_state.data_availability,
            },
            "factor_states": [
                {
                    "id": fs.id,
                    "name": fs.name,
                    "source": fs.source,
                    "category": fs.category,
                    "formula": fs.formula,
                    "dsl": fs.dsl,
                    "metrics": {
                        "ic": fs.metrics.ic,
                        "icir": fs.metrics.icir,
                        "turnover": fs.metrics.turnover,
                        "decay_halflife_days": fs.metrics.decay_halflife_days,
                        "n_obs": fs.metrics.n_obs,
                    },
                    "regime_applicability": fs.regime_applicability,
                }
                for fs in self.factor_states
            ],
            "regime_assessment": self.regime_assessment,
            "miner_outputs": self.miner_outputs,
            "screener_outputs": self.screener_outputs,
            "trader_outputs": self.trader_outputs,
            "evaluation": self.evaluation,
            "agent_history": self.agent_history,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> SharedMemoryH:
        """Reconstruct a SharedMemoryH from a dict (inverse of to_dict)."""
        ms_data = data["market_state"]
        market_state = MarketState(
            universe=ms_data["universe"],
            date_range=tuple(ms_data["date_range"]),
            regime_label=ms_data["regime_label"],
            crisis=ms_data["crisis"],
            macro_indicators=ms_data["macro_indicators"],
            data_availability=ms_data["data_availability"],
        )

        factor_states = [
            FactorState(
                id=fs["id"],
                name=fs["name"],
                source=fs["source"],
                category=fs["category"],
                formula=fs["formula"],
                dsl=fs["dsl"],
                metrics=FactorMetrics(
                    ic=fs["metrics"]["ic"],
                    icir=fs["metrics"]["icir"],
                    turnover=fs["metrics"]["turnover"],
                    decay_halflife_days=fs["metrics"]["decay_halflife_days"],
                    n_obs=fs["metrics"]["n_obs"],
                ),
                regime_applicability=fs["regime_applicability"],
            )
            for fs in data.get("factor_states", [])
        ]

        return cls(
            market_state=market_state,
            factor_states=factor_states,
            regime_assessment=data.get("regime_assessment", {}),
            miner_outputs=data.get("miner_outputs", {}),
            screener_outputs=data.get("screener_outputs", {}),
            trader_outputs=data.get("trader_outputs", {}),
            evaluation=data.get("evaluation", {}),
            agent_history=data.get("agent_history", []),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    @classmethod
    def create_initial(
        cls,
        universe: list[str],
        start_date: str,
        end_date: str,
        regime_label: str | None = None,
        crisis: bool = False,
        macro_indicators: dict[str, float] | None = None,
    ) -> SharedMemoryH:
        """Factory to create the initial SharedMemoryH for a pipeline run."""
        market_state = MarketState(
            universe=universe,
            date_range=(start_date, end_date),
            regime_label=regime_label,
            crisis=crisis,
            macro_indicators=macro_indicators or {},
            data_availability={},
        )
        return cls(market_state=market_state)

    def persist(self, db: Session, job_run_id: str | None = None) -> None:
        """Persist the serialised H into the ``AlphacrafterJobRun.shared_memory`` column.

        When *job_run_id* is provided, the full ``to_dict()`` output is stored
        in the job run's ``shared_memory`` JSON column (not ``progress_json``).
        This enables checkpoint/restore: after each agent completes, the
        orchestrator calls ``H.persist(db, job_run_id)`` so the frontend (or a
        crashed pipeline restart) can recover the latest state.
        """
        if not job_run_id:
            return

        from app.foundation.models.entities import AlphacrafterJobRun  # noqa: PLC0415 — avoid circular

        job_run = db.get(AlphacrafterJobRun, job_run_id)
        if job_run:
            job_run.shared_memory = self.to_dict()
            db.commit()
