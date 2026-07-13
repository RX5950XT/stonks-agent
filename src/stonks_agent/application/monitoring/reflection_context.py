"""Create a research-only reflection boundary over immutable outcome evidence."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import ValidationError

from stonks_agent.domain._trading import failure
from stonks_agent.domain.errors import ErrorCode, Result, Success
from stonks_agent.domain.monitoring import OutcomeEvidence, ReflectionContext
from stonks_agent.domain.research import ResearchArtifact, ResearchRequest
from stonks_agent.domain.risk import RiskDecision
from stonks_agent.domain.usage_budget import UsageBudget
from stonks_contracts.common import stable_payload_hash
from stonks_contracts.evidence import EvidenceItem, EvidenceKind


def build_reflection_context(
    *,
    request_id: UUID,
    run_id: UUID,
    decision: RiskDecision,
    outcome: OutcomeEvidence,
    outcome_evidence: EvidenceItem,
    tool_policy_id: str,
    model_policy_id: str,
    budget: UsageBudget,
    created_at: datetime,
    deadline_at: datetime,
) -> Result[ReflectionContext]:
    if not _history_matches(decision, outcome, outcome_evidence):
        return failure(ErrorCode.CONFLICT, "Reflection history binding changed")
    if created_at < outcome.calculated_at or deadline_at <= created_at:
        return failure(ErrorCode.INVALID_INPUT, "Reflection timeline is invalid")
    try:
        request = ResearchRequest(
            request_id=request_id,
            run_id=run_id,
            instrument_ids=frozenset(str(item) for item in outcome.instrument_ids),
            as_of=outcome_evidence.as_of,
            horizon_days=1,
            question=(
                "Reflect on this immutable paper outcome as research evidence only; "
                "do not alter the historical decision, target, risk checks, orders, "
                "fills, ledger, or execution state."
            ),
            allowed_evidence_ids=frozenset({outcome_evidence.evidence_id}),
            tool_policy_id=tool_policy_id,
            model_policy_id=model_policy_id,
            budget=budget,
            created_at=created_at,
            deadline_at=deadline_at,
        )
        context = ReflectionContext.create(
            historical_decision_id=decision.decision_id,
            historical_decision_hash=decision.decision_hash,
            outcome_id=outcome.outcome_id,
            outcome_hash=outcome.outcome_hash,
            outcome_evidence_id=outcome_evidence.evidence_id,
            outcome_evidence_hash=outcome_evidence.content_hash,
            request=request,
        )
    except (ValidationError, ValueError):
        return failure(ErrorCode.INVALID_INPUT, "Reflection context is invalid")
    return Success(context)


def accept_reflection_artifact(
    context: ReflectionContext,
    candidate: ResearchArtifact,
) -> Result[ResearchArtifact]:
    request = context.request
    if (
        candidate.request_id != request.request_id
        or candidate.run_id != request.run_id
        or candidate.instrument_ids != request.instrument_ids
        or candidate.as_of != request.as_of
        or candidate.allowed_evidence_ids != request.allowed_evidence_ids
        or not request.created_at <= candidate.created_at <= request.deadline_at
        or candidate.artifact_id
        in {
            context.historical_decision_id,
            context.outcome_id,
            request.request_id,
        }
    ):
        return failure(ErrorCode.CONFLICT, "Reflection artifact binding changed")
    cited = frozenset(
        evidence_id for claim in candidate.claims for evidence_id in claim.evidence_ids
    )
    if context.outcome_evidence_id not in cited:
        return failure(
            ErrorCode.MODEL_OUTPUT_INVALID,
            "Reflection artifact must cite outcome evidence",
        )
    return Success(candidate)


def _history_matches(
    decision: RiskDecision,
    outcome: OutcomeEvidence,
    evidence: EvidenceItem,
) -> bool:
    payload = outcome.model_dump(mode="json")
    return (
        decision.decision_hash == decision.expected_decision_hash()
        and outcome.historical_decision_id == decision.decision_id
        and outcome.historical_decision_hash == decision.decision_hash
        and outcome.account_id == decision.account_id
        and evidence.evidence_id == outcome.outcome_id
        and evidence.kind is EvidenceKind.DERIVED
        and evidence.payload == payload
        and evidence.content_hash == stable_payload_hash(payload)
        and evidence.raw_artifact_ref == f"sha256:{evidence.content_hash}"
        and evidence.available_at <= evidence.as_of
        and evidence.available_at <= created_cutoff(outcome)
    )


def created_cutoff(outcome: OutcomeEvidence) -> datetime:
    """Return the first instant at which the derived outcome may be consumed."""

    return outcome.calculated_at
