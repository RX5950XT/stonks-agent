"""Execute one fenced snapshot-scoped research job into a durable result."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid5

from stonks_agent.adapters.research.deterministic import (
    DeterministicResearchArtifactBuilder,
)
from stonks_agent.adapters.tools.evidence import (
    EvidenceTool,
    build_evidence_tool_policy,
)
from stonks_agent.application.research.orchestrate import orchestrate_research
from stonks_agent.application.research.pipeline import (
    DeterministicResearchPort,
    ReportRendererPort,
    TradingAgentsOpinionPort,
    run_research_pipeline,
)
from stonks_agent.domain.analysis_context import (
    AnalysisContext,
    AnalysisContextRequest,
    EvidenceRequirement,
)
from stonks_agent.domain.artifact import ArtifactMetadata
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.job import JobLease
from stonks_agent.domain.research import (
    AgentOpinion,
    ResearchArtifact,
    ResearchRequest,
)
from stonks_agent.domain.research_job import (
    KronosResearchOutcome,
    ResearchLeaseInput,
    ResearchWorkerResult,
)
from stonks_agent.domain.research_pipeline import (
    ResearchPipelineCommand,
    ResearchPipelineResult,
)
from stonks_agent.domain.tool_policy import ResearchPrincipal
from stonks_agent.domain.usage_budget import UsageBudget
from stonks_agent.ports.artifact_store import ArtifactManifest, ArtifactStore
from stonks_agent.ports.evidence_repository import EvidenceRepository
from stonks_agent.ports.llm import LLMPort
from stonks_agent.ports.operational_budget import OperationalBudgetEvaluatorPort
from stonks_agent.ports.research_completion import ResearchLeasePreflight
from stonks_agent.ports.research_forecast import ResearchForecastPort
from stonks_contracts.common import canonical_json
from stonks_contracts.evidence import EvidenceItem, EvidenceKind, Sensitivity


@dataclass(frozen=True, slots=True)
class ResearchLeaseProduct:
    result: ResearchWorkerResult
    manifest: ArtifactManifest


def process_research_lease(
    lease: JobLease,
    *,
    preflight: ResearchLeasePreflight,
    llm: LLMPort,
    artifacts: ArtifactStore,
    renderer: ReportRendererPort,
    budget: OperationalBudgetEvaluatorPort,
    forecast: ResearchForecastPort,
    clock: Callable[[], datetime],
) -> Result[ResearchLeaseProduct]:
    prepared = preflight.preflight(lease, now=clock())
    if isinstance(prepared, Failure):
        return prepared
    value = prepared.value
    request = _research_request(lease, value)
    if isinstance(request, Failure):
        return request
    repository = _ScopedEvidenceRepository(value.evidence)
    policy = build_evidence_tool_policy(
        instrument_ids=request.value.instrument_ids,
        evidence_ids=request.value.allowed_evidence_ids,
    )
    researched = orchestrate_research(
        request=request.value,
        evidence_items=value.evidence,
        principal=ResearchPrincipal(
            subject=lease.lease_owner,
            profile="research-worker",
            tool_policy_id=policy.policy_id,
        ),
        policy=policy,
        llm=llm,
        tool=EvidenceTool(
            repository=repository,
            artifacts=artifacts,
            as_of=value.request.as_of,
            clock=clock,
        ),
        artifacts=artifacts,
        builder=DeterministicResearchArtifactBuilder(),
        clock=clock,
        compact_store=artifacts,
        max_parallel_tools=1,
    )
    if isinstance(researched, Failure):
        return researched
    pipeline = _run_pipeline(
        lease,
        value,
        researched.value,
        repository=repository,
        llm=llm,
        renderer=renderer,
        artifacts=artifacts,
        budget=budget,
        clock=clock,
    )
    if isinstance(pipeline, Failure):
        return pipeline
    kronos = forecast.forecast(lease, value)
    outcome = (
        KronosResearchOutcome.failed(
            run_id=lease.run_id,
            snapshot_id=value.snapshot.snapshot_id,
            error_code=kronos.error.code,
        )
        if isinstance(kronos, Failure)
        else KronosResearchOutcome.forecast_succeeded(
            run_id=lease.run_id,
            snapshot_id=value.snapshot.snapshot_id,
            forecast_output=kronos.value,
        )
    )
    result = ResearchWorkerResult(
        schema_version="1.1.0",
        research_artifact=researched.value,
        pipeline=pipeline.value,
        kronos=outcome,
    )
    stored = artifacts.finalize(
        canonical_json(result.model_dump(mode="json")).encode("utf-8"),
        metadata=ArtifactMetadata(
            media_type="application/json",
            license_tag="Apache-2.0",
            sensitivity=Sensitivity.INTERNAL,
            source="stonks-agent-research-worker",
            attributes=(
                ("run_id", str(lease.run_id)),
                ("schema", f"research-worker-result/{result.schema_version}"),
            ),
        ),
        finalized_at=clock(),
    )
    if isinstance(stored, Failure):
        return stored
    return Success(ResearchLeaseProduct(result=result, manifest=stored.value))


def _research_request(
    lease: JobLease,
    value: ResearchLeaseInput,
) -> Result[ResearchRequest]:
    try:
        return Success(
            ResearchRequest(
                request_id=uuid5(lease.run_id, "bounded-research-request"),
                run_id=lease.run_id,
                instrument_ids=frozenset({value.request.instrument_id}),
                as_of=value.request.as_of,
                horizon_days=20,
                question=(
                    f"Research {value.request.symbol} using only cited snapshot "
                    "evidence. Inspect evidence with allowlisted tools before "
                    "returning claims, counterarguments, risks, and confidence."
                ),
                allowed_evidence_ids=frozenset(
                    item.evidence_id for item in value.evidence
                ),
                tool_policy_id="research-evidence-v1",
                model_policy_id=value.request.model_policy_id,
                budget=UsageBudget(
                    max_iterations=6,
                    max_tool_calls=16,
                    max_input_tokens=100_000,
                    max_output_tokens=4_096,
                    max_total_tokens=104_096,
                    max_cost_usd=Decimal("1"),
                    max_elapsed_ms=300_000,
                ),
                created_at=value.request.requested_at,
                deadline_at=lease.deadline_at,
            )
        )
    except ValueError:
        return _failure(
            ErrorCode.CONFIGURATION_INVALID,
            "Research lease configuration is invalid",
        )


def _run_pipeline(
    lease: JobLease,
    value: ResearchLeaseInput,
    research: ResearchArtifact,
    *,
    repository: EvidenceRepository,
    llm: LLMPort,
    renderer: ReportRendererPort,
    artifacts: ArtifactStore,
    budget: OperationalBudgetEvaluatorPort,
    clock: Callable[[], datetime],
) -> Result[ResearchPipelineResult]:
    command = ResearchPipelineCommand(
        run_id=lease.run_id,
        owner_subject=value.request.owner_subject,
        context_request=_context_request(lease, value),
        report_request_id=uuid5(lease.run_id, "report-request"),
        report_id=uuid5(lease.run_id, "report"),
        language=value.request.language,
        report_type="equity_research",
        model=f"policy:{value.request.model_policy_id}",
        policy_version=value.request.research_profile_id,
        max_output_tokens=4_096,
        deadline_at=lease.deadline_at,
    )
    return run_research_pipeline(
        command,
        repository=repository,
        deterministic=_PrecomputedResearch(research),
        tradingagents=_UnavailableTradingAgents(),
        llm=llm,
        renderer=renderer,
        artifacts=artifacts,
        budget=budget,
        clock=clock,
    )


def _context_request(
    lease: JobLease,
    value: ResearchLeaseInput,
) -> AnalysisContextRequest:
    evidence = value.evidence
    return AnalysisContextRequest(
        context_id=uuid5(lease.run_id, "analysis-context"),
        run_id=lease.run_id,
        subject=value.request.instrument_id,
        as_of=value.request.as_of,
        requirements=(
            EvidenceRequirement(
                capability="market",
                kinds=(EvidenceKind.MARKET_DATA,),
                required=True,
                minimum_items=1,
                maximum_items=len(evidence),
                freshness_seconds=None,
            ),
        ),
        allowed_sensitivities=tuple(
            sorted({item.sensitivity for item in evidence}, key=lambda item: item.value)
        ),
        allowed_license_tags=tuple(sorted({item.license_tag for item in evidence})),
        allowed_redistribution_tags=tuple(
            sorted({item.redistribution_tag for item in evidence})
        ),
    )


class _ScopedEvidenceRepository:
    def __init__(self, evidence: tuple[EvidenceItem, ...]) -> None:
        self._evidence = {item.evidence_id: item for item in evidence}

    def append(self, item: EvidenceItem) -> Result[EvidenceItem]:
        del item
        return _failure(ErrorCode.CAPABILITY_DENIED, "Evidence is read-only")

    def get(self, evidence_id: UUID) -> Result[EvidenceItem]:
        item = self._evidence.get(evidence_id)
        return (
            Success(item)
            if item is not None
            else _failure(ErrorCode.NOT_FOUND, "Evidence was not found")
        )

    def query_available(
        self,
        *,
        subject: str,
        as_of: datetime,
    ) -> Result[tuple[EvidenceItem, ...]]:
        values = tuple(
            item
            for item in self._evidence.values()
            if item.subject == subject and item.available_at <= as_of
        )
        return Success(tuple(sorted(values, key=lambda item: str(item.evidence_id))))


class _PrecomputedResearch(DeterministicResearchPort):
    def __init__(self, value: ResearchArtifact) -> None:
        self._value = value

    def research(self, context: AnalysisContext) -> Result[ResearchArtifact]:
        if (
            self._value.run_id != context.run_id
            or self._value.as_of != context.as_of
            or context.subject not in self._value.instrument_ids
        ):
            return _failure(ErrorCode.CONFLICT, "Research context changed")
        return Success(self._value)


class _UnavailableTradingAgents(TradingAgentsOpinionPort):
    def analyze(self, context: AnalysisContext) -> Result[AgentOpinion]:
        del context
        return _failure(
            ErrorCode.DATA_UNAVAILABLE,
            "TradingAgents is not composed in this runtime",
        )


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
