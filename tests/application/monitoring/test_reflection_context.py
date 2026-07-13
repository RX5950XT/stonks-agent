from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.application.monitoring.outcomes import (
    build_outcome,
    save_outcome_evidence,
)
from stonks_agent.application.monitoring.reflection_context import (
    accept_reflection_artifact,
    build_reflection_context,
)
from stonks_agent.domain.errors import Failure, Success
from stonks_agent.domain.research import (
    ResearchArtifact,
    ResearchClaim,
    ResearchClaimKind,
)
from stonks_agent.domain.usage_budget import UsageBudget, UsageConsumption

from .helpers import HASH_A, INSTRUMENT, decision
from .test_outcomes import command


def budget() -> UsageBudget:
    return UsageBudget(
        max_iterations=2,
        max_tool_calls=0,
        max_input_tokens=2_000,
        max_output_tokens=1_000,
        max_total_tokens=3_000,
        max_cost_usd=Decimal("1"),
        max_elapsed_ms=30_000,
    )


def context_result():  # type: ignore[no-untyped-def]
    outcome = build_outcome(command())
    assert isinstance(outcome, Success)
    stored = save_outcome_evidence(outcome.value, MemoryArtifactStore())
    assert isinstance(stored, Success)
    return build_reflection_context(
        request_id=UUID("87000000-0000-4000-8000-000000000001"),
        run_id=UUID("87000000-0000-4000-8000-000000000002"),
        decision=decision(),
        outcome=outcome.value,
        outcome_evidence=stored.value,
        tool_policy_id="reflection-read-only-v1",
        model_policy_id="reflection-model-v1",
        budget=budget(),
        created_at=outcome.value.calculated_at,
        deadline_at=outcome.value.calculated_at + timedelta(minutes=1),
    )


def artifact(context, **changes: object) -> ResearchArtifact:  # type: ignore[no-untyped-def]
    request = context.request
    payload: dict[str, object] = {
        "artifact_id": UUID("87000000-0000-4000-8000-000000000003"),
        "request_id": request.request_id,
        "run_id": request.run_id,
        "instrument_ids": request.instrument_ids,
        "as_of": request.as_of,
        "allowed_evidence_ids": request.allowed_evidence_ids,
        "claims": (
            ResearchClaim(
                claim_id=UUID("87000000-0000-4000-8000-000000000004"),
                kind=ResearchClaimKind.EVIDENCED,
                text="The immutable paper outcome underperformed its benchmark.",
                evidence_ids=request.allowed_evidence_ids,
            ),
        ),
        "confidence": Decimal("0.8"),
        "raw_output_artifact_ref": f"sha256:{HASH_A}",
        "producer": "bounded-reflection",
        "producer_version": "1.0.0",
        "usage": UsageConsumption(),
        "created_at": request.as_of,
    }
    return ResearchArtifact.model_validate(payload | changes)


def test_reflection_context_is_read_only_and_scoped_to_outcome_evidence() -> None:
    result = context_result()

    assert isinstance(result, Success)
    context = result.value
    assert context.request.instrument_ids == frozenset({str(INSTRUMENT)})
    assert context.request.allowed_evidence_ids == frozenset(
        {context.outcome_evidence_id}
    )
    assert "do not alter" in context.request.question.lower()
    assert context.historical_decision_hash == decision().decision_hash
    assert not {
        "target",
        "order",
        "quantity",
        "risk_override",
    } & set(ResearchArtifact.model_fields)


def test_accept_reflection_returns_only_new_bound_research_artifact() -> None:
    built = context_result()
    assert isinstance(built, Success)
    candidate = artifact(built.value)
    historical_hash = decision().decision_hash

    accepted = accept_reflection_artifact(built.value, candidate)

    assert isinstance(accepted, Success)
    assert accepted.value is candidate
    assert decision().decision_hash == historical_hash


def test_reflection_rejects_identity_or_evidence_scope_drift() -> None:
    built = context_result()
    assert isinstance(built, Success)
    candidate = artifact(built.value)
    wrong_run = artifact(
        built.value,
        artifact_id=UUID("87000000-0000-4000-8000-000000000005"),
        run_id=UUID("87000000-0000-4000-8000-000000000099"),
    )
    wrong_scope = artifact(
        built.value,
        artifact_id=UUID("87000000-0000-4000-8000-000000000006"),
        allowed_evidence_ids=frozenset(
            {*candidate.allowed_evidence_ids, UUID(int=999)}
        ),
        claims=(
            candidate.claims[0].model_copy(
                update={
                    "evidence_ids": frozenset(
                        {*candidate.allowed_evidence_ids, UUID(int=999)}
                    )
                }
            ),
        ),
    )

    assert isinstance(accept_reflection_artifact(built.value, wrong_run), Failure)
    assert isinstance(accept_reflection_artifact(built.value, wrong_scope), Failure)
