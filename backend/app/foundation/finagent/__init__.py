"""FinAgent context (bounded): budget-focused AI agents.

Budget, substitution, and explainer agents over the user's own expense data,
plus the RAG retriever over local knowledge and a LoRA fine-tuning scaffold.
Served to the UI via app.interface.api.finagent; RAGRetriever is also consumed by the
AlphaCrafter factor proposer.

This ``__init__`` is the context facade: it re-exports the callables other
contexts consume today. Import the package root, not internal submodules.
"""

from .budget_agent import BudgetAgent, BudgetInsight
from .explainer_agent import ExplainerAgent, ExplanationResponse
from .rag import RAGRetriever
from .substitution_agent import SubstitutionAgent, SubstitutionAgentResponse

__all__ = [
    "BudgetAgent",
    "BudgetInsight",
    "ExplainerAgent",
    "ExplanationResponse",
    "RAGRetriever",
    "SubstitutionAgent",
    "SubstitutionAgentResponse",
]
