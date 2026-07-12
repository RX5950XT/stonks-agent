"""Fail-closed canonical boundary around the isolated TradingAgents runtime."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from threading import RLock
from typing import Literal, Protocol, Self
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks_contracts.common import (
    ArtifactRef,
    ConfidenceCalibration,
    ModelUsage,
    UTCDateTime,
)
from stonks_contracts.research import AgentOpinion, AnalysisBundle

UPSTREAM_COMMIT = "01477f9afb7a47b849ed4c9259d3a9a4738d9fda"
WORKER_VERSION = "tradingagents-worker/0.1.0"


class WorkerProfile(StrEnum):
    PAPER = "paper"
    BACKTEST = "backtest"
    PRODUCTION = "production"


class EvidenceCategory(StrEnum):
    MARKET = "market"
    FUNDAMENTALS = "fundamentals"
    NEWS = "news"
    SENTIMENT = "sentiment"
    MACRO = "macro"


class ScopedEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: UUID
    artifact_ref: ArtifactRef
    available_at: UTCDateTime
    category: EvidenceCategory
    content: str = Field(min_length=1, max_length=32_768)
    untrusted_content: Literal[True] = True


class TradingAgentsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    run_id: UUID
    profile: WorkerProfile
    instrument_id: UUID
    symbol: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^[A-Z0-9][A-Z0-9.-]*$",
    )
    as_of: UTCDateTime
    horizon: str = Field(min_length=1, max_length=128)
    allowed_evidence_ids: tuple[UUID, ...] = Field(min_length=1, max_length=512)
    evidence: tuple[ScopedEvidence, ...] = Field(min_length=1, max_length=512)
    deadline: UTCDateTime

    @model_validator(mode="after")
    def validate_scope_and_time(self) -> Self:
        allowed = self.allowed_evidence_ids
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        if len(allowed) != len(set(allowed)):
            raise ValueError("allowed evidence ids must be unique")
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence items must be unique")
        if set(allowed) != set(evidence_ids):
            raise ValueError("evidence items must exactly match request scope")
        if any(item.available_at > self.as_of for item in self.evidence):
            raise ValueError("future evidence is not allowed")
        return self


class WorkerPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: WorkerProfile
    selected_analysts: tuple[
        Literal["market", "fundamentals", "news", "social"], ...
    ] = Field(min_length=1, max_length=4)
    max_evidence_bytes: int = Field(ge=1, le=16_777_216)
    network_egress: Literal["deny"] = "deny"
    worker_version: Literal["tradingagents-worker/0.1.0"] = WORKER_VERSION
    upstream_commit: Literal["01477f9afb7a47b849ed4c9259d3a9a4738d9fda"] = (
        UPSTREAM_COMMIT
    )

    @model_validator(mode="after")
    def validate_analysts(self) -> Self:
        if len(self.selected_analysts) != len(set(self.selected_analysts)):
            raise ValueError("selected analysts must be unique")
        return self


class RuntimeTelemetry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_usage: tuple[ModelUsage, ...] = Field(default_factory=tuple, max_length=128)
    tool_latency_ms: tuple[int, ...] = Field(default_factory=tuple, max_length=256)
    warnings: tuple[str, ...] = Field(default_factory=tuple, max_length=128)
    source_refs: tuple[UUID, ...] = Field(default_factory=tuple, max_length=512)


class RuntimeAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    recommendation: Literal["Buy", "Overweight", "Hold", "Underweight", "Sell"]
    thesis: str = Field(min_length=1, max_length=16_384)
    telemetry: RuntimeTelemetry


class TradingAgentsRuntime(Protocol):
    def run(self, request: TradingAgentsRequest) -> RuntimeAnalysis: ...


class WorkerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    analysis_bundle: AnalysisBundle
    agent_opinion: AgentOpinion


class WorkerError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    message: str = Field(min_length=1, max_length=256)


class WorkerSuccess(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: WorkerResponse


class WorkerFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    error: WorkerError


type WorkerResult = WorkerSuccess | WorkerFailure


class TradingAgentsWorker:
    __slots__ = ("_clock", "_lock", "_policy", "_runtime")

    def __init__(
        self,
        *,
        policy: WorkerPolicy,
        runtime: TradingAgentsRuntime,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._policy = policy
        self._runtime = runtime
        self._clock = clock or _utc_now
        self._lock = RLock()

    @property
    def policy(self) -> WorkerPolicy:
        return self._policy

    def analyze(self, request: TradingAgentsRequest) -> WorkerResult:
        preflight = self._preflight(request)
        if preflight is not None:
            return preflight
        try:
            with self._lock:
                analysis = self._runtime.run(request)
        except Exception:
            return _failure("runtime_failed", "TradingAgents runtime failed")
        if not set(analysis.telemetry.source_refs) <= set(request.allowed_evidence_ids):
            return _failure(
                "source_scope_exceeded",
                "TradingAgents output exceeded evidence scope",
            )
        if self._deadline_exceeded(request):
            return _failure("deadline_exceeded", "Worker deadline exceeded")
        return WorkerSuccess(value=_map_response(request, analysis, self._policy))

    def _preflight(self, request: TradingAgentsRequest) -> WorkerFailure | None:
        if request.profile is not self._policy.profile:
            return _failure("profile_mismatch", "Worker profile does not match request")
        if self._deadline_exceeded(request):
            return _failure("deadline_exceeded", "Worker deadline exceeded")
        total_bytes = sum(
            len(item.content.encode("utf-8")) for item in request.evidence
        )
        if total_bytes > self._policy.max_evidence_bytes:
            return _failure(
                "evidence_too_large", "Evidence context exceeds worker limit"
            )
        return None

    def _deadline_exceeded(self, request: TradingAgentsRequest) -> bool:
        now = self._clock()
        return now.tzinfo is None or now >= request.deadline


class WorkerEnvironment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: WorkerProfile
    model_proxy_token: str | None = Field(default=None, repr=False, exclude=True)


def validate_worker_environment(environment: Mapping[str, str]) -> WorkerEnvironment:
    forbidden_fragments = (
        "DATABASE",
        "POSTGRES",
        "BROKER",
        "REDIS",
        "QUEUE",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    )
    forbidden = tuple(
        key
        for key in environment
        if any(fragment in key.upper() for fragment in forbidden_fragments)
    )
    if forbidden:
        raise ValueError("forbidden worker environment variable is present")
    return WorkerEnvironment(
        profile=environment.get("STONKS_WORKER_PROFILE", "paper"),
        model_proxy_token=environment.get("STONKS_MODEL_PROXY_TOKEN"),
    )


def _map_response(
    request: TradingAgentsRequest,
    analysis: RuntimeAnalysis,
    policy: WorkerPolicy,
) -> WorkerResponse:
    warnings = tuple(
        dict.fromkeys(
            (
                *analysis.telemetry.warnings,
                *(
                    f"tool_latency_ms:{value}"
                    for value in analysis.telemetry.tool_latency_ms
                ),
                "upstream_confidence_unavailable",
                "upstream_execution_language_treated_as_research_opinion",
            )
        )
    )
    opinion_id = uuid5(request.request_id, "tradingagents:opinion")
    return WorkerResponse(
        analysis_bundle=AnalysisBundle(
            bundle_id=uuid5(request.run_id, "tradingagents:bundle"),
            run_id=request.run_id,
            as_of=request.as_of,
            analyst_artifact_ids=(),
            opinion_ids=(opinion_id,),
            source_refs=analysis.telemetry.source_refs,
            model_usage=analysis.telemetry.model_usage,
            warnings=warnings,
            worker_version=policy.worker_version,
        ),
        agent_opinion=AgentOpinion(
            opinion_id=opinion_id,
            instrument_id=request.instrument_id,
            as_of=request.as_of,
            horizon=request.horizon,
            recommendation=analysis.recommendation,
            thesis=analysis.thesis,
            confidence=Decimal("0"),
            calibration=ConfidenceCalibration.UNCALIBRATED,
            evidence_refs=analysis.telemetry.source_refs,
            producer="tradingagents-isolated-worker",
            model_version=policy.upstream_commit,
            warnings=warnings,
        ),
    )


def _failure(code: str, message: str) -> WorkerFailure:
    return WorkerFailure(error=WorkerError(code=code, message=message))


def _utc_now() -> datetime:
    return datetime.now(UTC)
