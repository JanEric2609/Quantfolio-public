"""Backward-compatible re-exports from the entities sub-module package.

Every symbol previously available from ``app.foundation.models.entities`` must be
importable from here so that existing ``from app.foundation.models.entities import X``
and ``from app.foundation.models import entities`` sites continue to work.
"""

from ._core import uuid_pk as uuid_pk, now_utc as now_utc, ASSET_TYPES as ASSET_TYPES
from .alphacrafter import (
    AlphaSignal as AlphaSignal,
    AlphacrafterJobRun as AlphacrafterJobRun,
    AlphacrafterTuningTrial as AlphacrafterTuningTrial,
    FactorsLibrary as FactorsLibrary,
    RecommendationDossier as RecommendationDossier,
    ScreenerRun as ScreenerRun,
    TraderBacktest as TraderBacktest,
)
from .advisor import AdvisorScorecard as AdvisorScorecard
from .advisor import AdvisorStrategy as AdvisorStrategy
from .advisor import StrategyLesson as StrategyLesson
from .advisor import AdvisorGraduationState as AdvisorGraduationState
from .advisor import AdvisorGraduationTransition as AdvisorGraduationTransition
from .analysis import (
    AnalysisReport as AnalysisReport,
    BacktestResult as BacktestResult,
    ExecutiveSummaryRecord as ExecutiveSummaryRecord,
)
from .asset import Asset as Asset
from .attribution import AttributionRun as AttributionRun
from .audit import AuditLog as AuditLog
from .backfill import BackfillJob as BackfillJob
from .broker import BrokerPosition as BrokerPosition, BrokerSyncLog as BrokerSyncLog
from .fx import FxRate as FxRate
from .auth import (
    PasswordResetToken as PasswordResetToken,
    PasskeyCredential as PasskeyCredential,
    User as User,
)
from .budget import (
    Category as Category,
    EnvelopeBudget as EnvelopeBudget,
    Expense as Expense,
    ExpenseRule as ExpenseRule,
    IncomeSource as IncomeSource,
    Invoice as Invoice,
    Subscription as Subscription,
    TelegramAccount as TelegramAccount,
    TelegramPairingCode as TelegramPairingCode,
)
from .chat import ChatMessage as ChatMessage, LlmAuditEvent as LlmAuditEvent
from .competition import (
    CompetitionDecision as CompetitionDecision,
    CompetitionRun as CompetitionRun,
)
from .discover import (
    AcTrialLedger as AcTrialLedger,
    DiscoverCandidate as DiscoverCandidate,
    DiscoverRun as DiscoverRun,
    DiscoveryConfig as DiscoveryConfig,
    DiscoveryConfigReview as DiscoveryConfigReview,
    DiscoveryPrediction as DiscoveryPrediction,
    DiscoverySkillSnapshot as DiscoverySkillSnapshot,
)
from .graduation import GraduationAssessment as GraduationAssessment
from .dkb import (
    DkbAccount as DkbAccount,
    DkbDiagnosticRun as DkbDiagnosticRun,
    DkbPosition as DkbPosition,
    DkbSyncLog as DkbSyncLog,
    DkbTransaction as DkbTransaction,
    PositionSnapshot as PositionSnapshot,
)
from .embedding import Embedding as Embedding, FinAgentRun as FinAgentRun
from .goal import Goal as Goal
from .jobs import JobRun as JobRun
from .market import (
    AnalystEstimateSnapshot as AnalystEstimateSnapshot,
    DataQualityEvent as DataQualityEvent,
    Fundamental as Fundamental,
    ListingCurrency as ListingCurrency,
    IndexCurrencyWeights as IndexCurrencyWeights,
    MacroIndicator as MacroIndicator,
    PriceCache as PriceCache,
)
from .mc import McPathSample as McPathSample, McRun as McRun
from .news import NewsItem as NewsItem, UserNewsRelevance as UserNewsRelevance
from .notification import Notification as Notification
from .nudge import Nudge as Nudge
from .paper_portfolio import (
    LlmAdviceCard as LlmAdviceCard,
    LlmPortfolioDecision as LlmPortfolioDecision,
    PaperCashFlow as PaperCashFlow,
    PaperHolding as PaperHolding,
    PaperPortfolio as PaperPortfolio,
    PaperPortfolioArchive as PaperPortfolioArchive,
    PaperSnapshot as PaperSnapshot,
    PaperTrade as PaperTrade,
)
from .metrics_snapshot import MetricsSnapshot as MetricsSnapshot
from .performance import (
    Composite as Composite,
    CompositeMembership as CompositeMembership,
    PerformanceLedgerEntry as PerformanceLedgerEntry,
)
from .portfolio import (
    ActivityLedgerEntry as ActivityLedgerEntry,
    Benchmark as Benchmark,
    BookPositionSnapshot as BookPositionSnapshot,
    ConnectedAccount as ConnectedAccount,
    Holding as Holding,
    Portfolio as Portfolio,
    PortfolioSnapshot as PortfolioSnapshot,
    ShadowPosition as ShadowPosition,
    TransactionLog as TransactionLog,
)
from .provider import ProviderHealth as ProviderHealth
from .quant import (
    QuantExperiment as QuantExperiment,
    QuantExperimentRun as QuantExperimentRun,
    QuantFactorScore as QuantFactorScore,
    QuantMlModel as QuantMlModel,
    QuantRlPolicy as QuantRlPolicy,
    QuantRun as QuantRun,
    QuantSavedScenario as QuantSavedScenario,
    QuantSignal as QuantSignal,
    StrategyGraveyardEntry as StrategyGraveyardEntry,
)
from .recommendation import (
    Recommendation as Recommendation,
    RecommendationAttempt as RecommendationAttempt,
    RecommendationOutcome as RecommendationOutcome,
    RecommendationReview as RecommendationReview,
)
from .regime import (
    FactorIcTracking as FactorIcTracking,
    RegimeLabelHistory as RegimeLabelHistory,
    RegimeRecommendationWeight as RegimeRecommendationWeight,
)
from .research import (
    MultiHorizonVerdict as MultiHorizonVerdict,
    StockResearchReport as StockResearchReport,
)
from .rl import MetaPolicySnapshot as MetaPolicySnapshot, QValue as QValue
from .security import (
    Security as Security,
    SecurityAlias as SecurityAlias,
    SecurityListing as SecurityListing,
)
from .settings import ApiKey as ApiKey, AppSetting as AppSetting
from .target_allocation import TargetAllocation as TargetAllocation
from .trial_ledger import TrialLedgerEntry as TrialLedgerEntry
from .factor_evidence import EvidenceGateRun as EvidenceGateRun, FactorEvidenceCard as FactorEvidenceCard
from .tax import (
    TaxLedgerEvent as TaxLedgerEvent,
    TaxLot as TaxLot,
    TaxResidencyPeriod as TaxResidencyPeriod,
)
from .verification import VerificationAlert as VerificationAlert
from .watchlist import WatchlistItem as WatchlistItem
