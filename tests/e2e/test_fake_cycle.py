from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from stonks_agent.adapters.fakes.platform import build_fake_run_service
from stonks_agent.application.workflows.run_cycle import (
    IdempotencyConflict,
    RunCycleRequest,
)

AS_OF = datetime(2026, 1, 2, 21, 0, tzinfo=UTC)


def request(
    key: str,
    *,
    account_id: str = "paper-main",
    symbol: str = "AAPL",
    available_at: datetime = AS_OF,
) -> RunCycleRequest:
    return RunCycleRequest(
        idempotency_key=key,
        account_id=account_id,
        instrument_id=f"instrument-{symbol.lower()}",
        symbol=symbol,
        as_of=AS_OF,
        evidence_available_at=available_at,
        signal_value=Decimal("0.80"),
        signal_confidence=Decimal("0.90"),
    )


def test_complete_cycle_uses_next_bar_and_balanced_journal() -> None:
    service = build_fake_run_service(clock=AS_OF, seed="complete-cycle")

    result = service.run(request("cycle-001"))

    assert result.status == "completed"
    assert result.portfolio_target.target_weight == Decimal("0.05")
    assert result.risk_decision.approved is True
    assert result.reservation.status == "consumed"
    assert result.order_intent.reservation_id == result.reservation.reservation_id
    assert result.execution_receipt.fill is not None
    assert result.execution_receipt.fill.bar_time > AS_OF
    assert result.execution_receipt.fill.price == Decimal("101.00")
    assert result.execution_receipt.fill.quantity == Decimal("50")
    assert result.journal_transaction.is_balanced()
    assert sum(
        posting.signed_amount
        for posting in result.journal_transaction.postings
        if posting.commodity == "USD"
    ) == Decimal("0")
    assert result.report.evidence_refs == (result.evidence_id,)
    assert result.events[-1].event_type == "run.completed"
    assert len(result.control_hash) == 64
    assert service.replay(result.run_id).projection_hash == result.projection_hash


def test_same_idempotency_key_returns_same_result_without_new_events() -> None:
    service = build_fake_run_service(clock=AS_OF, seed="idempotent")
    cycle_request = request("same-key")

    first = service.run(cycle_request)
    event_count = service.event_count
    second = service.run(cycle_request)

    assert second == first
    assert service.event_count == event_count


def test_independent_replay_runs_have_identical_control_plane_hashes() -> None:
    cycle_request = request("deterministic-replay")

    first = build_fake_run_service(clock=AS_OF, seed="replay-seed").run(cycle_request)
    second = build_fake_run_service(clock=AS_OF, seed="replay-seed").run(cycle_request)

    assert second.control_hash == first.control_hash
    assert second.projection_hash == first.projection_hash
    assert second.events == first.events


def test_same_idempotency_key_with_different_payload_fails_closed() -> None:
    service = build_fake_run_service(clock=AS_OF, seed="conflict")
    service.run(request("same-key", symbol="AAPL"))

    with pytest.raises(IdempotencyConflict):
        service.run(request("same-key", symbol="MSFT"))


def test_future_evidence_is_rejected_without_reservation_or_fill() -> None:
    service = build_fake_run_service(clock=AS_OF, seed="future-evidence")

    result = service.run(
        request("future", available_at=AS_OF + timedelta(seconds=1))
    )

    assert result.status == "rejected"
    assert result.risk_decision.approved is False
    assert result.reservation is None
    assert result.order_intent is None
    assert result.execution_receipt is None
    assert result.journal_transaction is None
    assert "future_evidence" in result.risk_decision.reasons
    assert result.events[-1].event_type == "run.completed"


def test_concurrent_cycles_cannot_double_spend_or_duplicate_position() -> None:
    service = build_fake_run_service(
        clock=AS_OF,
        seed="concurrent",
        initial_cash=Decimal("10000.00"),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                service.run,
                [request("concurrent-a"), request("concurrent-b")],
            )
        )

    snapshot = service.account_snapshot("paper-main")
    fills = [
        result.execution_receipt.fill
        for result in results
        if result.execution_receipt and result.execution_receipt.fill
    ]
    assert len(fills) == 1
    assert snapshot.cash >= Decimal("0")
    assert snapshot.positions["instrument-aapl"] == Decimal("5")
    assert snapshot.open_reservations == ()
    assert service.journal_is_balanced("paper-main")
