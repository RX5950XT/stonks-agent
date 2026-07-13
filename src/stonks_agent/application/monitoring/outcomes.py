"""Calculate and persist immutable paper outcome evidence."""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

from pydantic import ValidationError

from stonks_agent.domain._trading import failure
from stonks_agent.domain.artifact import ArtifactMetadata
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_agent.domain.monitoring import BuildOutcomeCommand, OutcomeEvidence
from stonks_agent.ports.artifact_store import ArtifactStore
from stonks_contracts.common import canonical_json
from stonks_contracts.evidence import EvidenceItem, EvidenceKind, Sensitivity
from stonks_contracts.market_data import DataQuality, DataQualityStatus

_RATIO_QUANTUM = Decimal("0.000000000001")


def build_outcome(command: BuildOutcomeCommand) -> Result[OutcomeEvidence]:
    invalid = _validate_bindings(command)
    if invalid is not None:
        return invalid
    start, end = command.valuations[0], command.valuations[-1]
    if start.nav <= 0:
        return failure(ErrorCode.INVALID_INPUT, "Outcome baseline NAV must be positive")
    fee_delta = end.cumulative_fees - start.cumulative_fees
    fill_fees = sum((item.fees for item in command.fill_refs), Decimal(0))
    if fee_delta < 0 or fee_delta != fill_fees:
        return failure(ErrorCode.CONFLICT, "Outcome fees do not reconcile")
    raw_return = _ratio(end.nav, start.nav)
    benchmark_return = _ratio(
        command.benchmark_end.price, command.benchmark_start.price
    )
    target = command.decision.normalized_target
    assert target is not None
    source_ids = {
        command.benchmark_start.evidence_id,
        command.benchmark_end.evidence_id,
        *(
            item.mark.evidence_id
            for value in command.valuations
            for item in value.positions
        ),
    }
    try:
        provisional = OutcomeEvidence.model_construct(
            outcome_id=command.outcome_id,
            account_id=command.decision.account_id,
            historical_decision_id=command.decision.decision_id,
            historical_decision_hash=command.decision.decision_hash,
            historical_target_id=target.target_id,
            historical_target_hash=target.calculation_hash,
            instrument_ids=tuple(
                sorted((item.instrument_id for item in target.allocations), key=str)
            ),
            valuations=command.valuations,
            benchmark_start=command.benchmark_start,
            benchmark_end=command.benchmark_end,
            raw_return=raw_return,
            benchmark_return=benchmark_return,
            benchmark_alpha=_quantize(raw_return - benchmark_return),
            max_drawdown=_max_drawdown(command),
            fee_currency=start.base_currency,
            fees=fee_delta,
            fill_refs=command.fill_refs,
            source_evidence_ids=tuple(sorted(source_ids, key=str)),
            calculated_at=command.calculated_at,
            outcome_hash="0" * 64,
        )
        value = OutcomeEvidence.model_validate(
            provisional.model_dump(mode="python")
            | {"outcome_hash": provisional.expected_outcome_hash()}
        )
    except (ValidationError, ValueError):
        return failure(ErrorCode.CONFLICT, "Outcome output failed integrity checks")
    return Success(value)


def save_outcome_evidence(
    outcome: OutcomeEvidence,
    artifacts: ArtifactStore,
) -> Result[EvidenceItem]:
    payload = outcome.model_dump(mode="json")
    try:
        stored = artifacts.finalize(
            canonical_json(payload).encode("utf-8"),
            metadata=ArtifactMetadata(
                media_type="application/json",
                license_tag="Apache-2.0",
                sensitivity=Sensitivity.INTERNAL,
                source="stonks-agent",
                attributes=(("schema", "paper-outcome/1.0.0"),),
            ),
            finalized_at=outcome.calculated_at,
        )
    except Exception:
        return failure(ErrorCode.INTERNAL_ERROR, "Outcome artifact storage failed")
    if isinstance(stored, Failure):
        return stored
    end = outcome.valuations[-1]
    try:
        evidence = EvidenceItem(
            evidence_id=outcome.outcome_id,
            subject=outcome.account_id,
            kind=EvidenceKind.DERIVED,
            payload=payload,
            event_time=end.as_of,
            published_at=outcome.calculated_at,
            available_at=outcome.calculated_at,
            observed_at=outcome.calculated_at,
            as_of=outcome.calculated_at,
            source="stonks-agent-paper-monitoring",
            provider="stonks-agent",
            content_hash=stored.value.content_hash,
            raw_artifact_ref=f"sha256:{stored.value.content_hash}",
            quality=DataQuality(
                status=DataQualityStatus.AVAILABLE,
                completeness=Decimal(1),
            ),
            sensitivity=Sensitivity.INTERNAL,
            license_tag="Apache-2.0",
            redistribution_tag="internal-only",
            derived_from=outcome.source_evidence_ids,
            transformation_version="paper-outcome/1.0.0",
            untrusted_content=False,
        )
    except ValidationError:
        return failure(ErrorCode.INTERNAL_ERROR, "Outcome evidence is invalid")
    return Success(evidence)


def _validate_bindings(command: BuildOutcomeCommand) -> Failure | None:
    decision = command.decision
    if (
        not decision.approved
        or decision.normalized_target is None
        or decision.decision_hash != decision.expected_decision_hash()
    ):
        return failure(ErrorCode.CONFLICT, "Outcome requires an approved decision")
    valuations = command.valuations
    times = tuple(item.as_of for item in valuations)
    if times != tuple(sorted(times)) or len(times) != len(set(times)):
        return failure(
            ErrorCode.CONFLICT, "Outcome valuations are not strictly ordered"
        )
    sequences = tuple(item.ledger_sequence for item in valuations)
    if sequences != tuple(sorted(sequences)):
        return failure(ErrorCode.CONFLICT, "Outcome ledger sequence moved backwards")
    if any(
        item.account_id != decision.account_id
        or item.base_currency != valuations[0].base_currency
        for item in valuations
    ):
        return failure(ErrorCode.CONFLICT, "Outcome valuation binding changed")
    if (
        decision.decided_at > valuations[0].as_of
        or valuations[-1].as_of > command.calculated_at
    ):
        return failure(ErrorCode.CONFLICT, "Outcome timeline is invalid")
    benchmark = _benchmark_error(command)
    if benchmark is not None:
        return benchmark
    return _fill_error(command)


def _benchmark_error(command: BuildOutcomeCommand) -> Failure | None:
    start, end = command.valuations[0], command.valuations[-1]
    first, last = command.benchmark_start, command.benchmark_end
    if (
        first.instrument_id != last.instrument_id
        or first.currency != start.base_currency
        or last.currency != start.base_currency
        or first.available_at > start.as_of
        or last.available_at > end.as_of
        or first.event_time > start.as_of
        or last.event_time > end.as_of
    ):
        return failure(ErrorCode.CONFLICT, "Outcome benchmark is future or mismatched")
    return None


def _fill_error(command: BuildOutcomeCommand) -> Failure | None:
    decision = command.decision
    start, end = command.valuations[0], command.valuations[-1]
    target_ids = {item.instrument_id for item in decision.normalized_target.allocations}  # type: ignore[union-attr]
    fill_ids = tuple(str(item.fill_id) for item in command.fill_refs)
    if fill_ids != tuple(sorted(fill_ids)) or len(fill_ids) != len(set(fill_ids)):
        return failure(ErrorCode.CONFLICT, "Outcome fills must be unique and sorted")
    for item in command.fill_refs:
        if (
            item.risk_decision_id != decision.decision_id
            or item.risk_decision_hash != decision.decision_hash
            or item.account_id != decision.account_id
            or item.instrument_id not in target_ids
            or item.fee_currency != start.base_currency
            or not start.as_of < item.occurred_at <= end.as_of
        ):
            return failure(ErrorCode.CONFLICT, "Outcome fill binding changed")
    return None


def _ratio(end: Decimal, start: Decimal) -> Decimal:
    return _quantize(end / start - 1)


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_RATIO_QUANTUM, rounding=ROUND_HALF_EVEN)


def _max_drawdown(command: BuildOutcomeCommand) -> Decimal:
    peak = command.valuations[0].nav
    drawdown = Decimal(0)
    for item in command.valuations:
        peak = max(peak, item.nav)
        drawdown = min(drawdown, item.nav / peak - 1)
    return _quantize(drawdown)
