from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.fills import ExecutionReceipt, Fill
from stonks_agent.domain.journal import (
    JournalPosting,
    JournalSide,
    JournalTransaction,
    verify_journal_chain,
)
from stonks_agent.domain.orders import (
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
    append_order_event,
    build_execution_command,
    create_order_intent,
)
from stonks_agent.domain.portfolio import (
    AccountPortfolioSnapshot,
    CashBalance,
    PortfolioTarget,
    PositionBalance,
    TargetAllocation,
)
from stonks_agent.domain.reservations import (
    ReservationKind,
    ReservationState,
    consume_reservation,
    create_reservation,
    expire_reservation,
    release_reservation,
)
from stonks_agent.domain.risk import RiskCheck, RiskDecision
from stonks_agent.ports.ledger import LedgerHead

NOW = datetime(2026, 7, 13, 8, tzinfo=UTC)
ACCOUNT_ID = "paper-account"
INSTRUMENT_ID = UUID("41000000-0000-4000-8000-000000000001")
SNAPSHOT_ID = UUID("41000000-0000-4000-8000-000000000002")
TARGET_ID = UUID("41000000-0000-4000-8000-000000000003")
DECISION_ID = UUID("41000000-0000-4000-8000-000000000004")
RESERVATION_ID = UUID("41000000-0000-4000-8000-000000000005")
INTENT_ID = UUID("41000000-0000-4000-8000-000000000006")
COMMAND_ID = UUID("41000000-0000-4000-8000-000000000007")
FILL_ID = UUID("41000000-0000-4000-8000-000000000008")
HASH_A = "a" * 64
HASH_B = "b" * 64


def snapshot() -> AccountPortfolioSnapshot:
    return AccountPortfolioSnapshot(
        snapshot_id=SNAPSHOT_ID,
        account_id=ACCOUNT_ID,
        as_of=NOW,
        account_aggregate_sequence=7,
        portfolio_sequence=3,
        ledger_sequence=11,
        ledger_hash=HASH_A,
        cash=(
            CashBalance(
                currency="USD",
                settled_amount=Decimal("10000.00"),
                reserved_amount=Decimal("250.00"),
                quantum=Decimal("0.01"),
            ),
        ),
        positions=(
            PositionBalance(
                instrument_id=INSTRUMENT_ID,
                quantity=Decimal("10"),
                sellable_quantity=Decimal("8"),
                reserved_quantity=Decimal("2"),
                quantum=Decimal("1"),
            ),
        ),
        pending_order_ids=(),
    )


def target() -> PortfolioTarget:
    return PortfolioTarget.create(
        target_id=TARGET_ID,
        account_id=ACCOUNT_ID,
        portfolio_snapshot_id=SNAPSHOT_ID,
        account_aggregate_sequence=7,
        portfolio_sequence=3,
        as_of=NOW,
        allocations=(
            TargetAllocation(
                instrument_id=INSTRUMENT_ID,
                current_quantity=Decimal("10"),
                target_quantity=Decimal("14"),
                delta_quantity=Decimal("4"),
                quantity_quantum=Decimal("1"),
                target_weight=Decimal("0.25"),
            ),
        ),
        input_signal_ids=(UUID("41000000-0000-4000-8000-000000000009"),),
        policy_version="portfolio-v1",
        policy_hash=HASH_B,
        expected_turnover=Decimal("0.10"),
        expected_cost=Decimal("1.25"),
        cost_currency="USD",
    )


def decision(*, approved: bool = True) -> RiskDecision:
    return RiskDecision.create(
        decision_id=DECISION_ID,
        target=target(),
        approved=approved,
        normalized_target=target() if approved else None,
        checks=(
            RiskCheck(
                code="cash_available",
                passed=approved,
                actual=Decimal("9750.00"),
                limit=Decimal("405.00"),
                reason=None if approved else "insufficient_cash",
            ),
        ),
        policy_version="risk-v1",
        policy_hash=HASH_A,
        decided_at=NOW + timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=6),
    )


def reservation():
    result = create_reservation(
        reservation_id=RESERVATION_ID,
        order_intent_id=INTENT_ID,
        decision=decision(),
        kind=ReservationKind.CASH,
        commodity="USD",
        amount=Decimal("405.00"),
        quantum=Decimal("0.01"),
        instrument_id=INSTRUMENT_ID,
        at=NOW + timedelta(minutes=2),
        expires_at=NOW + timedelta(minutes=5),
        current_account_sequence=7,
        current_portfolio_sequence=3,
    )
    assert isinstance(result, Success)
    return result.value.reservation


def intent():
    result = create_order_intent(
        intent_id=INTENT_ID,
        run_id=UUID("41000000-0000-4000-8000-000000000010"),
        decision=decision(),
        reservation=reservation(),
        instrument_id=INSTRUMENT_ID,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("4"),
        quantity_quantum=Decimal("1"),
        limit_price=Decimal("100.00"),
        stop_price=None,
        time_in_force=TimeInForce.DAY,
        valid_from=NOW + timedelta(minutes=2),
        valid_until=NOW + timedelta(minutes=5),
        idempotency_key="paper-account:run:order-1",
        execution_model_version="paper-v1",
        created_at=NOW + timedelta(minutes=2),
    )
    assert isinstance(result, Success)
    return result.value


def fill(*, quantity: Decimal = Decimal("4")) -> Fill:
    return Fill(
        fill_id=FILL_ID,
        command_id=COMMAND_ID,
        order_intent_id=INTENT_ID,
        account_id=ACCOUNT_ID,
        instrument_id=INSTRUMENT_ID,
        side=OrderSide.BUY,
        quantity=quantity,
        quantity_quantum=Decimal("1"),
        price=Decimal("100.00"),
        price_quantum=Decimal("0.01"),
        fee_currency="USD",
        fees=Decimal("1.00"),
        slippage=Decimal("0.02"),
        occurred_at=NOW + timedelta(minutes=3),
        simulator_ref="paper-v1:bar-1",
    )


def test_portfolio_snapshot_and_target_use_exact_available_balances_and_hash() -> None:
    state = snapshot()
    built = target()

    assert state.cash[0].available_amount == Decimal("9750.00")
    assert state.positions[0].available_quantity == Decimal("6")
    assert built.calculation_hash == built.expected_calculation_hash()
    assert built.allocations[0].delta_quantity == Decimal("4")


@given(cents=st.integers(min_value=1, max_value=1_000_000))
def test_decimal_quantization_rejects_hidden_fractional_cash(cents: int) -> None:
    valid = Decimal(cents) / Decimal("100")
    assert (
        CashBalance(
            currency="USD",
            settled_amount=valid,
            reserved_amount=Decimal("0.00"),
            quantum=Decimal("0.01"),
        ).available_amount
        == valid
    )
    with pytest.raises(ValidationError):
        CashBalance(
            currency="USD",
            settled_amount=valid + Decimal("0.001"),
            reserved_amount=Decimal("0.00"),
            quantum=Decimal("0.01"),
        )


def test_portfolio_rejects_duplicate_or_unsorted_instruments_and_wrong_delta() -> None:
    allocation = target().allocations[0]
    other = allocation.model_copy(
        update={"instrument_id": UUID("41000000-0000-4000-8000-000000000000")}
    )
    with pytest.raises(ValidationError):
        PortfolioTarget.model_validate(
            target().model_dump() | {"allocations": (allocation, allocation)}
        )
    with pytest.raises(ValidationError):
        PortfolioTarget.model_validate(
            target().model_dump() | {"allocations": (allocation, other)}
        )
    with pytest.raises(ValidationError):
        TargetAllocation(
            instrument_id=INSTRUMENT_ID,
            current_quantity=Decimal("10"),
            target_quantity=Decimal("14"),
            delta_quantity=Decimal("3"),
            quantity_quantum=Decimal("1"),
            target_weight=Decimal("0.25"),
        )


def test_risk_approval_requires_all_checks_and_exact_target_sequence_binding() -> None:
    approved = decision()
    assert approved.is_current(
        account_aggregate_sequence=7,
        portfolio_sequence=3,
        at=NOW + timedelta(minutes=2),
    )
    assert not approved.is_current(
        account_aggregate_sequence=8,
        portfolio_sequence=3,
        at=NOW + timedelta(minutes=2),
    )
    assert not approved.is_current(
        account_aggregate_sequence=7,
        portfolio_sequence=4,
        at=NOW + timedelta(minutes=2),
    )
    assert not approved.is_current(
        account_aggregate_sequence=7,
        portfolio_sequence=3,
        at=approved.expires_at,
    )
    with pytest.raises(ValidationError):
        RiskDecision.model_validate(
            approved.model_dump()
            | {"checks": (RiskCheck(code="cash", passed=False, reason="rejected"),)}
        )


def test_risk_approval_alone_cannot_build_execution_command() -> None:
    order = intent()
    result = build_execution_command(
        command_id=COMMAND_ID,
        intent=order,
        decision=decision(),
        reservation=None,
        current_account_sequence=8,
        current_portfolio_sequence=3,
        attempt_generation=1,
        attempt_nonce="nonce-1",
        issued_at=NOW + timedelta(minutes=3),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT


def test_reservation_requires_fresh_risk_then_advances_account_sequence() -> None:
    created = reservation()

    assert created.state is ReservationState.OPEN
    assert created.risk_account_aggregate_sequence == 7
    assert created.account_aggregate_sequence == 8
    assert created.remaining_amount == Decimal("405.00")
    assert created.event_sequence == 1
    assert created.previous_event_hash is None

    stale = create_reservation(
        reservation_id=RESERVATION_ID,
        order_intent_id=INTENT_ID,
        decision=decision(),
        kind=ReservationKind.CASH,
        commodity="USD",
        amount=Decimal("405.00"),
        quantum=Decimal("0.01"),
        instrument_id=INSTRUMENT_ID,
        at=NOW + timedelta(minutes=2),
        expires_at=NOW + timedelta(minutes=5),
        current_account_sequence=8,
        current_portfolio_sequence=3,
    )
    assert isinstance(stale, Failure)
    assert stale.error.code is ErrorCode.CONFLICT


def test_reservation_partial_consume_release_and_terminal_state_machine() -> None:
    created = reservation()
    first = consume_reservation(
        created,
        amount=Decimal("100.00"),
        at=NOW + timedelta(minutes=3),
    )
    assert isinstance(first, Success)
    assert first.value.reservation.state is ReservationState.PARTIALLY_CONSUMED
    assert first.value.reservation.remaining_amount == Decimal("305.00")
    assert first.value.event.previous_event_hash == created.event_hash

    released = release_reservation(
        first.value.reservation,
        at=NOW + timedelta(minutes=4),
        reason="order_cancelled",
    )
    assert isinstance(released, Success)
    assert released.value.reservation.state is ReservationState.RELEASED
    assert released.value.reservation.remaining_amount == Decimal("0.00")
    assert isinstance(
        consume_reservation(
            released.value.reservation,
            amount=Decimal("1.00"),
            at=NOW + timedelta(minutes=4),
        ),
        Failure,
    )


def test_order_intent_and_command_require_exact_risk_reservation_binding() -> None:
    order = intent()
    command = build_execution_command(
        command_id=COMMAND_ID,
        intent=order,
        decision=decision(),
        reservation=reservation(),
        current_account_sequence=8,
        current_portfolio_sequence=3,
        attempt_generation=1,
        attempt_nonce="nonce-1",
        issued_at=NOW + timedelta(minutes=3),
    )

    assert isinstance(command, Success)
    assert command.value.intent.account_aggregate_sequence == 8
    assert command.value.reservation_hash == reservation().event_hash
    stale = build_execution_command(
        command_id=COMMAND_ID,
        intent=order,
        decision=decision(),
        reservation=reservation(),
        current_account_sequence=9,
        current_portfolio_sequence=3,
        attempt_generation=1,
        attempt_nonce="nonce-1",
        issued_at=NOW + timedelta(minutes=3),
    )
    assert isinstance(stale, Failure)
    assert stale.error.code is ErrorCode.CONFLICT


def test_order_event_state_machine_is_monotonic_and_hash_chained() -> None:
    accepted = append_order_event(
        intent(),
        previous=None,
        target_status=OrderStatus.ACCEPTED,
        cumulative_filled_quantity=Decimal("0"),
        occurred_at=NOW + timedelta(minutes=3),
    )
    assert isinstance(accepted, Success)
    partial = append_order_event(
        intent(),
        previous=accepted.value,
        target_status=OrderStatus.PARTIALLY_FILLED,
        cumulative_filled_quantity=Decimal("2"),
        occurred_at=NOW + timedelta(minutes=4),
    )
    assert isinstance(partial, Success)
    completed = append_order_event(
        intent(),
        previous=partial.value,
        target_status=OrderStatus.FILLED,
        cumulative_filled_quantity=Decimal("4"),
        occurred_at=NOW + timedelta(minutes=4, seconds=30),
    )
    assert isinstance(completed, Success)
    assert completed.value.sequence == 3
    assert completed.value.previous_event_hash == partial.value.event_hash
    assert completed.value.remaining_quantity == 0

    invalid = append_order_event(
        intent(),
        previous=completed.value,
        target_status=OrderStatus.ACCEPTED,
        cumulative_filled_quantity=Decimal("4"),
        occurred_at=NOW + timedelta(minutes=6),
    )
    assert isinstance(invalid, Failure)
    assert invalid.error.code is ErrorCode.CONFLICT


def test_receipt_rejects_overfill_and_requires_fill_totals_to_match_event() -> None:
    accepted = append_order_event(
        intent(),
        previous=None,
        target_status=OrderStatus.ACCEPTED,
        cumulative_filled_quantity=Decimal("0"),
        occurred_at=NOW + timedelta(minutes=3),
    )
    assert isinstance(accepted, Success)
    filled = append_order_event(
        intent(),
        previous=accepted.value,
        target_status=OrderStatus.FILLED,
        cumulative_filled_quantity=Decimal("4"),
        occurred_at=NOW + timedelta(minutes=4),
    )
    assert isinstance(filled, Success)
    receipt = ExecutionReceipt.create(
        receipt_id=UUID("41000000-0000-4000-8000-000000000011"),
        command_id=COMMAND_ID,
        intent=intent(),
        event=filled.value,
        fills=(fill(),),
        occurred_at=NOW + timedelta(minutes=4),
    )
    assert receipt.receipt_hash == receipt.expected_receipt_hash()
    with pytest.raises(ValidationError):
        ExecutionReceipt.create(
            receipt_id=UUID("41000000-0000-4000-8000-000000000012"),
            command_id=COMMAND_ID,
            intent=intent(),
            event=filled.value,
            fills=(fill(quantity=Decimal("5")),),
            occurred_at=NOW + timedelta(minutes=4),
        )


@given(units=st.integers(min_value=1, max_value=1_000_000))
def test_balanced_journal_property_and_deterministic_hash(units: int) -> None:
    amount = Decimal(units) / Decimal("100")
    transaction = JournalTransaction.create(
        transaction_id=UUID("41000000-0000-4000-8000-000000000013"),
        account_id=ACCOUNT_ID,
        sequence=1,
        occurred_at=NOW,
        previous_hash=None,
        source_order_intent_id=INTENT_ID,
        source_fill_id=FILL_ID,
        postings=(
            JournalPosting(
                posting_id=UUID("41000000-0000-4000-8000-000000000014"),
                ledger_account="asset:cash:USD",
                commodity="USD",
                side=JournalSide.DEBIT,
                amount=amount,
                quantum=Decimal("0.01"),
            ),
            JournalPosting(
                posting_id=UUID("41000000-0000-4000-8000-000000000015"),
                ledger_account="clearing:paper:USD",
                commodity="USD",
                side=JournalSide.CREDIT,
                amount=amount,
                quantum=Decimal("0.01"),
            ),
        ),
    )

    assert transaction.is_balanced()
    assert transaction.transaction_hash == transaction.expected_transaction_hash()
    assert verify_journal_chain((transaction,))


def test_journal_rejects_unbalanced_mixed_quantum_and_invalid_chain() -> None:
    debit = JournalPosting(
        posting_id=UUID("41000000-0000-4000-8000-000000000014"),
        ledger_account="asset:cash:USD",
        commodity="USD",
        side=JournalSide.DEBIT,
        amount=Decimal("10.00"),
        quantum=Decimal("0.01"),
    )
    with pytest.raises(ValidationError):
        JournalTransaction.create(
            transaction_id=UUID("41000000-0000-4000-8000-000000000013"),
            account_id=ACCOUNT_ID,
            sequence=1,
            occurred_at=NOW,
            previous_hash=None,
            source_order_intent_id=INTENT_ID,
            source_fill_id=FILL_ID,
            postings=(
                debit,
                debit.model_copy(
                    update={
                        "posting_id": UUID("41000000-0000-4000-8000-000000000015"),
                        "side": JournalSide.CREDIT,
                        "amount": Decimal("9.99"),
                    }
                ),
            ),
        )
    with pytest.raises(ValidationError):
        JournalPosting.model_validate(
            debit.model_dump() | {"amount": Decimal("10.001")}
        )


def test_snapshot_and_ledger_head_require_exact_genesis_hash_shape() -> None:
    genesis = AccountPortfolioSnapshot(
        snapshot_id=SNAPSHOT_ID,
        account_id=ACCOUNT_ID,
        as_of=NOW,
        account_aggregate_sequence=0,
        portfolio_sequence=0,
        ledger_sequence=0,
        ledger_hash=None,
    )
    assert genesis.ledger_hash is None
    assert LedgerHead(account_id=ACCOUNT_ID, sequence=0).transaction_hash is None
    with pytest.raises(ValidationError):
        LedgerHead(account_id=ACCOUNT_ID, sequence=0, transaction_hash=HASH_A)
    with pytest.raises(ValidationError):
        LedgerHead(account_id=ACCOUNT_ID, sequence=1, transaction_hash=None)


def test_reservation_invalid_inputs_are_structured_and_expiry_is_terminal() -> None:
    invalid_amount = create_reservation(
        reservation_id=RESERVATION_ID,
        order_intent_id=INTENT_ID,
        decision=decision(),
        kind=ReservationKind.CASH,
        commodity="USD",
        amount=Decimal("0"),
        quantum=Decimal("0.01"),
        instrument_id=INSTRUMENT_ID,
        at=NOW + timedelta(minutes=2),
        expires_at=NOW + timedelta(minutes=5),
        current_account_sequence=7,
        current_portfolio_sequence=3,
    )
    too_long = create_reservation(
        reservation_id=RESERVATION_ID,
        order_intent_id=INTENT_ID,
        decision=decision(),
        kind=ReservationKind.CASH,
        commodity="USD",
        amount=Decimal("405.00"),
        quantum=Decimal("0.01"),
        instrument_id=INSTRUMENT_ID,
        at=NOW + timedelta(minutes=2),
        expires_at=NOW + timedelta(minutes=7),
        current_account_sequence=7,
        current_portfolio_sequence=3,
    )
    expired = expire_reservation(reservation(), at=NOW + timedelta(minutes=5))

    assert isinstance(invalid_amount, Failure)
    assert invalid_amount.error.code is ErrorCode.INVALID_INPUT
    assert isinstance(too_long, Failure)
    assert isinstance(expired, Success)
    assert expired.value.reservation.state is ReservationState.EXPIRED
    assert isinstance(
        release_reservation(
            expired.value.reservation,
            at=NOW + timedelta(minutes=6),
            reason="too_late",
        ),
        Failure,
    )


def test_order_genesis_event_cannot_precede_intent_and_reject_is_terminal() -> None:
    before_intent = append_order_event(
        intent(),
        previous=None,
        target_status=OrderStatus.ACCEPTED,
        cumulative_filled_quantity=Decimal("0"),
        occurred_at=NOW + timedelta(minutes=1),
    )
    rejected = append_order_event(
        intent(),
        previous=None,
        target_status=OrderStatus.REJECTED,
        cumulative_filled_quantity=Decimal("0"),
        occurred_at=NOW + timedelta(minutes=3),
        reason="unsupported_session",
    )

    assert isinstance(before_intent, Failure)
    assert isinstance(rejected, Success)
    assert isinstance(
        append_order_event(
            intent(),
            previous=rejected.value,
            target_status=OrderStatus.ACCEPTED,
            cumulative_filled_quantity=Decimal("0"),
            occurred_at=NOW + timedelta(minutes=4),
        ),
        Failure,
    )


def test_receipt_rejects_fill_with_wrong_account_instrument_or_side() -> None:
    accepted = append_order_event(
        intent(),
        previous=None,
        target_status=OrderStatus.ACCEPTED,
        cumulative_filled_quantity=Decimal("0"),
        occurred_at=NOW + timedelta(minutes=3),
    )
    assert isinstance(accepted, Success)
    filled = append_order_event(
        intent(),
        previous=accepted.value,
        target_status=OrderStatus.FILLED,
        cumulative_filled_quantity=Decimal("4"),
        occurred_at=NOW + timedelta(minutes=4),
    )
    assert isinstance(filled, Success)
    wrong_fill = fill().model_copy(update={"account_id": "another-account"})

    with pytest.raises(ValidationError):
        ExecutionReceipt.create(
            receipt_id=UUID("41000000-0000-4000-8000-000000000012"),
            command_id=COMMAND_ID,
            intent=intent(),
            event=filled.value,
            fills=(wrong_fill,),
            occurred_at=NOW + timedelta(minutes=4),
        )


def test_journal_requires_stable_posting_order_quantum_and_valid_chain() -> None:
    debit = JournalPosting(
        posting_id=UUID("41000000-0000-4000-8000-000000000014"),
        ledger_account="asset:cash:USD",
        commodity="USD",
        side=JournalSide.DEBIT,
        amount=Decimal("10.00"),
        quantum=Decimal("0.01"),
    )
    credit = JournalPosting(
        posting_id=UUID("41000000-0000-4000-8000-000000000015"),
        ledger_account="clearing:paper:USD",
        commodity="USD",
        side=JournalSide.CREDIT,
        amount=Decimal("10.00"),
        quantum=Decimal("0.01"),
    )
    with pytest.raises(ValidationError):
        JournalTransaction.create(
            transaction_id=UUID("41000000-0000-4000-8000-000000000013"),
            account_id=ACCOUNT_ID,
            sequence=1,
            occurred_at=NOW,
            previous_hash=None,
            source_order_intent_id=INTENT_ID,
            source_fill_id=FILL_ID,
            postings=(credit, debit),
        )
    with pytest.raises(ValidationError):
        JournalTransaction.create(
            transaction_id=UUID("41000000-0000-4000-8000-000000000013"),
            account_id=ACCOUNT_ID,
            sequence=1,
            occurred_at=NOW,
            previous_hash=None,
            source_order_intent_id=INTENT_ID,
            source_fill_id=FILL_ID,
            postings=(debit, credit.model_copy(update={"quantum": Decimal("0.001")})),
        )

    first = JournalTransaction.create(
        transaction_id=UUID("41000000-0000-4000-8000-000000000013"),
        account_id=ACCOUNT_ID,
        sequence=1,
        occurred_at=NOW,
        previous_hash=None,
        source_order_intent_id=INTENT_ID,
        source_fill_id=FILL_ID,
        postings=(debit, credit),
    )
    second = JournalTransaction.create(
        transaction_id=UUID("41000000-0000-4000-8000-000000000016"),
        account_id=ACCOUNT_ID,
        sequence=2,
        occurred_at=NOW + timedelta(minutes=1),
        previous_hash=first.transaction_hash,
        source_order_intent_id=INTENT_ID,
        source_fill_id=UUID("41000000-0000-4000-8000-000000000017"),
        postings=(
            debit.model_copy(
                update={"posting_id": UUID("41000000-0000-4000-8000-000000000018")}
            ),
            credit.model_copy(
                update={"posting_id": UUID("41000000-0000-4000-8000-000000000019")}
            ),
        ),
    )
    assert verify_journal_chain((first, second))
    tampered = second.model_construct(**second.model_dump() | {"previous_hash": HASH_B})
    assert not verify_journal_chain((first, tampered))
