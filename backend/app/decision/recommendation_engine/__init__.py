"""Investment Recommendation Engine.

Synthesizes data from all Quantfolio services into fact-grounded
buy/sell/hold recommendations with source attribution.
"""
from app.decision.recommendation_engine.models import (
    ContextBundle,
    DataHealth,
    Evidence,
    RecommendationItem,
    RecommendationReport,
    SourceHealth,
)
from app.decision.recommendation_engine.orchestrator import generate_recommendations
from app.decision.recommendation_engine.v2_adapter import recommendation_item_to_v2

__all__ = [
    "ContextBundle",
    "DataHealth",
    "Evidence",
    "RecommendationItem",
    "RecommendationReport",
    "SourceHealth",
    "generate_recommendations",
    "recommendation_item_to_v2",
]
