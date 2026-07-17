"""Compose PIT evidence, dual research, report generation, and rendering."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from stonks_agent.application.reporting.evidence_assembler import (
    assemble_evidence_context,
)
from stonks_agent.application.reporting.generate import generate_report
from stonks_agent.application.telemetry import record_operation
from stonks_agent.domain.analysis_context import AnalysisContext
from stonks_agent.domain.artifact import ArtifactMetadata
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    Success,
)
from stonks_agent.domain.operational_budget import BudgetScope, BudgetStatus
from stonks_agent.domain.report import GenerateReportRequest
from stonks_agent.domain.research import AgentOpinion, ResearchArtifact
from stonks_agent.domain.research_pipeline import (
    PipelineIssue,
    PipelineStage,
    PipelineStatus,
    ResearchPipelineCommand,
    ResearchPipelineResult,
)
from stonks_agent.domain.telemetry import ComponentName, OperationName
from stonks_agent.ports.artifact_store import ArtifactStore
from stonks_agent.ports.evidence_repository import EvidenceRepository
from stonks_agent.ports.llm import LLMPort
from stonks_agent.ports.operational_budget import OperationalBudgetEvaluatorPort
from stonks_agent.ports.telemetry import OperationRecorderPort
from stonks_contracts.common import canonical_json
from stonks_contracts.evidence import Sensitivity
from stonks_contracts.report import AnalysisReport


class DeterministicResearchPort(Protocol):
    def research(self, context: AnalysisContext) -> Result[ResearchArtifact]: ...


class TradingAgentsOpinionPort(Protocol):
    def analyze(self, context: AnalysisContext) -> Result[AgentOpinion]: ...


class ReportRendererPort(Protocol):
    def render(self, report: AnalysisReport) -> Result[AnalysisReport]: ...


def run_research_pipeline(
    command: ResearchPipelineCommand,
    *,
    repository: EvidenceRepository,
    deterministic: DeterministicResearchPort,
    tradingagents: TradingAgentsOpinionPort,
    llm: LLMPort,
    renderer: ReportRendererPort,
    artifacts: ArtifactStore,
    budget: OperationalBudgetEvaluatorPort,
    clock: Callable[[], datetime],
    telemetry: OperationRecorderPort | None = None,
) -> Result[ResearchPipelineResult]:
    if clock() >= command.deadline_at:
        return _finish(
            command,
            PipelineStatus.FAILED,
            None,
            None,
            None,
            (_issue(PipelineStage.DEADLINE, ErrorCode.DEADLINE_EXCEEDED),),
            artifacts,
            clock,
        )
    budget_stop = _stop_for_budget(
        command,
        budget=budget,
        research=None,
        opinion=None,
        issues=(),
        artifacts=artifacts,
        clock=clock,
    )
    if budget_stop is not None:
        return budget_stop
    assembled = record_operation(
        telemetry,
        component=ComponentName.PROVIDER,
        operation=OperationName.FETCH,
        call=lambda: assemble_evidence_context(command.context_request, repository),
    )
    if isinstance(assembled, Failure):
        return _finish(
            command,
            PipelineStatus.FAILED,
            None,
            None,
            None,
            (_from_failure(PipelineStage.CONTEXT, assembled),),
            artifacts,
            clock,
        )
    context = assembled.value
    budget_stop = _stop_for_budget(
        command,
        budget=budget,
        research=None,
        opinion=None,
        issues=(),
        artifacts=artifacts,
        clock=clock,
    )
    if budget_stop is not None:
        return budget_stop
    researched = record_operation(
        telemetry,
        component=ComponentName.MODEL,
        operation=OperationName.INFER,
        call=lambda: deterministic.research(context),
    )
    if isinstance(researched, Failure):
        return _finish(
            command,
            PipelineStatus.FAILED,
            None,
            None,
            None,
            (_from_failure(PipelineStage.DETERMINISTIC, researched),),
            artifacts,
            clock,
        )
    research_artifact = researched.value
    invalid_research = _validate_research(command, context, research_artifact)
    if invalid_research is not None:
        return _finish(
            command,
            PipelineStatus.FAILED,
            None,
            None,
            None,
            (invalid_research,),
            artifacts,
            clock,
        )
    budget_stop = _stop_for_budget(
        command,
        budget=budget,
        research=research_artifact,
        opinion=None,
        issues=(),
        artifacts=artifacts,
        clock=clock,
    )
    if budget_stop is not None:
        return budget_stop
    issues: tuple[PipelineIssue, ...] = ()
    opinion: AgentOpinion | None = None
    analyzed = record_operation(
        telemetry,
        component=ComponentName.MODEL,
        operation=OperationName.INFER,
        call=lambda: tradingagents.analyze(context),
    )
    if isinstance(analyzed, Failure):
        issues = (_from_failure(PipelineStage.TRADINGAGENTS, analyzed),)
    else:
        candidate = analyzed.value
        invalid_opinion = _validate_opinion(command, context, candidate)
        if invalid_opinion is None:
            opinion = candidate
        else:
            issues = (invalid_opinion,)
    budget_stop = _stop_for_budget(
        command,
        budget=budget,
        research=research_artifact,
        opinion=opinion,
        issues=issues,
        artifacts=artifacts,
        clock=clock,
    )
    if budget_stop is not None:
        return budget_stop
    signal_ids = tuple(
        sorted(
            (research_artifact.artifact_id,)
            + ((opinion.opinion_id,) if opinion is not None else ()),
            key=str,
        )
    )
    report_request = GenerateReportRequest(
        request_id=command.report_request_id,
        report_id=command.report_id,
        run_id=command.run_id,
        owner_subject=command.owner_subject,
        context=context,
        language=command.language,
        report_type=command.report_type,
        model=command.model,
        policy_version=command.policy_version,
        signal_ids=signal_ids,
        max_output_tokens=command.max_output_tokens,
        deadline_at=command.deadline_at,
    )
    generated = record_operation(
        telemetry,
        component=ComponentName.LLM,
        operation=OperationName.GENERATE,
        call=lambda: generate_report(report_request, llm),
    )
    if isinstance(generated, Failure):
        return _finish(
            command,
            PipelineStatus.FAILED,
            research_artifact,
            opinion,
            None,
            (*issues, _from_failure(PipelineStage.REPORT, generated)),
            artifacts,
            clock,
        )
    budget_stop = _stop_for_budget(
        command,
        budget=budget,
        research=research_artifact,
        opinion=opinion,
        issues=issues,
        artifacts=artifacts,
        clock=clock,
    )
    if budget_stop is not None:
        return budget_stop
    rendered = record_operation(
        telemetry,
        component=ComponentName.DELIVERY,
        operation=OperationName.GENERATE,
        call=lambda: renderer.render(generated.value),
    )
    if isinstance(rendered, Failure):
        return _finish(
            command,
            PipelineStatus.FAILED,
            research_artifact,
            opinion,
            None,
            (*issues, _from_failure(PipelineStage.RENDER, rendered)),
            artifacts,
            clock,
        )
    status = PipelineStatus.DEGRADED if issues else PipelineStatus.SUCCEEDED
    return _finish(
        command,
        status,
        research_artifact,
        opinion,
        rendered.value,
        issues,
        artifacts,
        clock,
    )


def _stop_for_budget(
    command: ResearchPipelineCommand,
    *,
    budget: OperationalBudgetEvaluatorPort,
    research: ResearchArtifact | None,
    opinion: AgentOpinion | None,
    issues: tuple[PipelineIssue, ...],
    artifacts: ArtifactStore,
    clock: Callable[[], datetime],
) -> Result[ResearchPipelineResult] | None:
    try:
        decision = budget.evaluate(BudgetScope.RESEARCH)
    except Exception:
        decision = None
    if decision is not None and decision.status is BudgetStatus.WITHIN:
        return None
    status = (
        PipelineStatus.DEGRADED
        if decision is not None and decision.status is BudgetStatus.DEGRADED
        else PipelineStatus.FAILED
    )
    return _finish(
        command,
        status,
        research,
        opinion,
        None,
        (*issues, _issue(PipelineStage.BUDGET, ErrorCode.BUDGET_EXHAUSTED)),
        artifacts,
        clock,
    )


def _validate_research(
    command: ResearchPipelineCommand,
    context: AnalysisContext,
    artifact: ResearchArtifact,
) -> PipelineIssue | None:
    evidence_ids = frozenset(item.evidence_id for item in context.evidence)
    invalid = (
        artifact.run_id != command.run_id
        or artifact.as_of != context.as_of
        or context.subject not in artifact.instrument_ids
        or not artifact.allowed_evidence_ids <= evidence_ids
    )
    return _issue(PipelineStage.DETERMINISTIC, ErrorCode.CONFLICT) if invalid else None


def _validate_opinion(
    command: ResearchPipelineCommand,
    context: AnalysisContext,
    opinion: AgentOpinion,
) -> PipelineIssue | None:
    evidence_ids = frozenset(item.evidence_id for item in context.evidence)
    invalid = (
        opinion.as_of != context.as_of
        or opinion.instrument_id != context.subject
        or not opinion.evidence_ids <= evidence_ids
        or opinion.artifact_id.int == 0
        or command.run_id != context.run_id
    )
    return _issue(PipelineStage.TRADINGAGENTS, ErrorCode.CONFLICT) if invalid else None


def _finish(
    command: ResearchPipelineCommand,
    status: PipelineStatus,
    research: ResearchArtifact | None,
    opinion: AgentOpinion | None,
    report: AnalysisReport | None,
    issues: tuple[PipelineIssue, ...],
    artifacts: ArtifactStore,
    clock: Callable[[], datetime],
) -> Result[ResearchPipelineResult]:
    audit = {
        "schema": "research-pipeline-audit/1.0.0",
        "run_id": str(command.run_id),
        "context_id": str(command.context_request.context_id),
        "status": status.value,
        "research_artifact_id": str(research.artifact_id) if research else None,
        "opinion_id": str(opinion.opinion_id) if opinion else None,
        "report_id": str(report.report_id) if report else None,
        "report_hash": report.payload_hash() if report else None,
        "rendering_hashes": [item.content_hash for item in report.renderings]
        if report
        else [],
        "issues": [item.model_dump(mode="json") for item in issues],
    }
    stored = artifacts.finalize(
        canonical_json(audit).encode("utf-8"),
        metadata=ArtifactMetadata(
            media_type="application/json",
            license_tag="Apache-2.0",
            sensitivity=Sensitivity.INTERNAL,
            source="stonks-agent-research-pipeline",
            attributes=(
                ("schema", "research-pipeline-audit/1.0.0"),
                ("run_id", str(command.run_id)),
            ),
        ),
        finalized_at=clock(),
    )
    if isinstance(stored, Failure):
        return stored
    return Success(
        ResearchPipelineResult(
            run_id=command.run_id,
            status=status,
            context_id=command.context_request.context_id,
            research_artifact_id=research.artifact_id if research else None,
            opinion_id=opinion.opinion_id if opinion else None,
            report=report,
            issues=issues,
            audit_artifact_ref=f"sha256:{stored.value.content_hash}",
        )
    )


def _from_failure(stage: PipelineStage, failure: Failure) -> PipelineIssue:
    return _issue(stage, failure.error.code)


def _issue(stage: PipelineStage, code: ErrorCode) -> PipelineIssue:
    return PipelineIssue(stage=stage, code=code.value)
