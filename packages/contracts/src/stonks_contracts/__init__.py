"""Versioned, execution-safe wire contracts for Stonks Agent."""

from .common import SCHEMA_VERSION, ContractModel, ModelUsage, Money, stable_payload_hash
from .evidence import EvidenceItem, EvidencePack
from .execution import (
    ExecutionCommand,
    ExecutionReceipt,
    Fill,
    JournalPosting,
    JournalTransaction,
    OrderIntent,
)
from .instrument import InstrumentKey, ProviderSymbol
from .market_data import Bar, BarSeries, DataQuality, DatasetSnapshot, MarketDataQuery
from .portfolio import (
    CashBalance,
    MarketPrice,
    PortfolioSnapshot,
    PortfolioTarget,
    Position,
    TargetAllocation,
)
from .report import AnalysisReport, ReportRendering
from .research import (
    AgentOpinion,
    AnalysisBundle,
    Citation,
    ResearchArtifact,
    ResearchClaim,
    ResearchRequest,
)
from .risk import AccountReservation, RiskCheck, RiskDecision
from .signal import AlphaSignal, ForecastSignal
from .workflow import Run, RunEvent

CANONICAL_CHAIN = (
    "EvidenceItem/ResearchArtifact",
    "AnalysisBundle/AgentOpinion/AlphaSignal/ForecastSignal",
    "PortfolioTarget",
    "RiskDecision",
    "AccountReservation",
    "OrderIntent",
    "ExecutionReceipt/Fill",
    "JournalTransaction",
    "AnalysisReport",
)

SCHEMA_MODELS: tuple[type[ContractModel], ...] = (
    Money,
    ModelUsage,
    ProviderSymbol,
    InstrumentKey,
    DataQuality,
    MarketDataQuery,
    Bar,
    BarSeries,
    DatasetSnapshot,
    EvidenceItem,
    EvidencePack,
    ResearchRequest,
    ResearchClaim,
    Citation,
    ResearchArtifact,
    AnalysisBundle,
    AgentOpinion,
    ForecastSignal,
    AlphaSignal,
    CashBalance,
    Position,
    MarketPrice,
    PortfolioSnapshot,
    TargetAllocation,
    PortfolioTarget,
    RiskCheck,
    RiskDecision,
    AccountReservation,
    OrderIntent,
    ExecutionCommand,
    ExecutionReceipt,
    Fill,
    JournalPosting,
    JournalTransaction,
    Run,
    RunEvent,
    ReportRendering,
    AnalysisReport,
)

__all__ = [
    "CANONICAL_CHAIN",
    "SCHEMA_MODELS",
    "SCHEMA_VERSION",
    "AccountReservation",
    "AgentOpinion",
    "AlphaSignal",
    "AnalysisBundle",
    "AnalysisReport",
    "BarSeries",
    "ContractModel",
    "DatasetSnapshot",
    "EvidenceItem",
    "EvidencePack",
    "ExecutionCommand",
    "ExecutionReceipt",
    "Fill",
    "ForecastSignal",
    "InstrumentKey",
    "JournalPosting",
    "JournalTransaction",
    "MarketDataQuery",
    "OrderIntent",
    "PortfolioSnapshot",
    "PortfolioTarget",
    "ResearchArtifact",
    "RiskDecision",
    "stable_payload_hash",
]
