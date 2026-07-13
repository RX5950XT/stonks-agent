from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.application.monitoring.outcomes import (
    build_outcome,
    save_outcome_evidence,
)
from stonks_agent.domain.errors import Failure, Success
from stonks_agent.domain.monitoring import (
    BuildOutcomeCommand,
    OutcomeFillReference,
)
from stonks_agent.domain.orders import OrderSide
from stonks_contracts.common import stable_payload_hash
from stonks_contracts.evidence import EvidenceKind

from .helpers import (
    ACCOUNT_ID,
    BENCHMARK,
    HASH_A,
    HASH_B,
    INSTRUMENT,
    NOW,
    decision,
    mark,
    valuation,
)


def fill_ref(**changes: object) -> OutcomeFillReference:
    risk = decision()
    payload: dict[str, object] = {
        "risk_decision_id": risk.decision_id,
        "risk_decision_hash": risk.decision_hash,
        "account_id": ACCOUNT_ID,
        "receipt_id": UUID("85000000-0000-4000-8000-000000000001"),
        "receipt_hash": HASH_A,
        "order_intent_id": UUID("85000000-0000-4000-8000-000000000002"),
        "intent_hash": HASH_B,
        "fill_id": UUID("85000000-0000-4000-8000-000000000003"),
        "instrument_id": INSTRUMENT,
        "side": OrderSide.BUY,
        "quantity": Decimal("10"),
        "price": Decimal("100"),
        "fee_currency": "USD",
        "fees": Decimal("2.00"),
        "occurred_at": NOW + timedelta(hours=1),
    }
    return OutcomeFillReference.model_validate(payload | changes)


def command(**changes: object) -> BuildOutcomeCommand:
    start = valuation(
        identifier=1,
        at=NOW,
        nav=Decimal("10000.00"),
        fees=Decimal("0.00"),
        ledger_sequence=1,
    )
    trough = valuation(
        identifier=2,
        at=NOW + timedelta(minutes=30),
        nav=Decimal("9500.00"),
        fees=Decimal("0.00"),
        ledger_sequence=1,
    )
    end = valuation(
        identifier=3,
        at=NOW + timedelta(hours=2),
        nav=Decimal("10098.00"),
        fees=Decimal("2.00"),
        ledger_sequence=2,
    )
    payload: dict[str, object] = {
        "outcome_id": UUID("86000000-0000-4000-8000-000000000001"),
        "decision": decision(),
        "valuations": (start, trough, end),
        "benchmark_start": mark(
            instrument_id=BENCHMARK,
            price=Decimal("100"),
            at=NOW,
        ),
        "benchmark_end": mark(
            instrument_id=BENCHMARK,
            price=Decimal("105"),
            at=NOW + timedelta(hours=2),
        ),
        "fill_refs": (fill_ref(),),
        "calculated_at": NOW + timedelta(hours=2, seconds=1),
    }
    return BuildOutcomeCommand.model_validate(payload | changes)


def test_build_outcome_records_return_alpha_drawdown_fees_and_fills() -> None:
    result = build_outcome(command())

    assert isinstance(result, Success)
    assert result.value.raw_return == Decimal("0.009800000000")
    assert result.value.benchmark_return == Decimal("0.050000000000")
    assert result.value.benchmark_alpha == Decimal("-0.040200000000")
    assert result.value.max_drawdown == Decimal("-0.050000000000")
    assert result.value.fees == Decimal("2.00")
    assert result.value.fill_refs == (fill_ref(),)
    assert result.value.outcome_hash == result.value.expected_outcome_hash()


def test_build_outcome_fails_closed_on_fee_or_decision_binding_drift() -> None:
    fee_drift = build_outcome(command(fill_refs=(fill_ref(fees=Decimal("1.00")),)))
    other_decision = build_outcome(
        command(fill_refs=(fill_ref(risk_decision_hash="f" * 64),))
    )

    assert isinstance(fee_drift, Failure)
    assert isinstance(other_decision, Failure)


def test_save_outcome_evidence_finalizes_exact_immutable_payload() -> None:
    built = build_outcome(command())
    assert isinstance(built, Success)
    store = MemoryArtifactStore()

    first = save_outcome_evidence(built.value, store)
    replay = save_outcome_evidence(built.value, store)

    assert isinstance(first, Success)
    assert replay == first
    evidence = first.value
    assert evidence.evidence_id == built.value.outcome_id
    assert evidence.kind is EvidenceKind.DERIVED
    assert evidence.content_hash == stable_payload_hash(evidence.payload)
    assert evidence.raw_artifact_ref == f"sha256:{evidence.content_hash}"
    assert evidence.derived_from == built.value.source_evidence_ids


def test_outcome_rejects_non_monotonic_valuation_or_future_benchmark() -> None:
    valid = command()
    reversed_values = build_outcome(
        command(valuations=(valid.valuations[1], valid.valuations[0]))
    )
    future = build_outcome(
        command(
            benchmark_end=valid.benchmark_end.model_copy(
                update={
                    "available_at": valid.valuations[-1].as_of + timedelta(seconds=1)
                }
            )
        )
    )

    assert isinstance(reversed_values, Failure)
    assert isinstance(future, Failure)
