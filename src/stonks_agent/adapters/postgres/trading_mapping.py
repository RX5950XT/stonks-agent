"""Pure ORM/domain mappings for PostgreSQL paper-trading persistence."""

from __future__ import annotations

from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from stonks_agent.adapters.postgres.models import (
    AccountReservationRow,
    JournalPostingRow,
    JournalTransactionRow,
    OrderEventRow,
    OrderIntentRow,
    PaperAccountEventRow,
    PaperCashProjectionRow,
    PaperFillRow,
    PaperPositionProjectionRow,
    PortfolioTargetRow,
    ReservationEventRow,
    RiskDecisionRow,
)
from stonks_agent.domain.fills import Fill
from stonks_agent.domain.journal import JournalPosting, JournalSide, JournalTransaction
from stonks_agent.domain.orders import OrderEvent, OrderIntent, OrderStatus
from stonks_agent.domain.portfolio import (
    AccountPortfolioSnapshot,
    CashBalance,
    PaperAccountEvent,
    PaperAccountState,
    PositionBalance,
)
from stonks_agent.domain.reservations import (
    AccountReservation,
    ReservationEvent,
    ReservationEventType,
    ReservationKind,
    ReservationState,
)
from stonks_agent.domain.risk import RiskDecision
from stonks_contracts.common import stable_payload_hash


def cash_row(
    snapshot: AccountPortfolioSnapshot, balance: CashBalance
) -> PaperCashProjectionRow:
    return PaperCashProjectionRow(
        account_id=snapshot.account_id,
        currency=balance.currency,
        settled_amount=balance.settled_amount,
        reserved_amount=balance.reserved_amount,
        quantum=balance.quantum,
        updated_sequence=0,
        updated_at=snapshot.as_of,
    )


def position_row(
    snapshot: AccountPortfolioSnapshot, balance: PositionBalance
) -> PaperPositionProjectionRow:
    return PaperPositionProjectionRow(
        account_id=snapshot.account_id,
        instrument_id=balance.instrument_id,
        quantity=balance.quantity,
        sellable_quantity=balance.sellable_quantity,
        reserved_quantity=balance.reserved_quantity,
        quantum=balance.quantum,
        updated_sequence=0,
        updated_at=snapshot.as_of,
    )


def cash_from_row(row: PaperCashProjectionRow) -> CashBalance:
    return CashBalance(
        currency=row.currency,
        settled_amount=row.settled_amount,
        reserved_amount=row.reserved_amount,
        quantum=row.quantum,
    )


def position_from_row(row: PaperPositionProjectionRow) -> PositionBalance:
    return PositionBalance(
        instrument_id=row.instrument_id,
        quantity=row.quantity,
        sellable_quantity=row.sellable_quantity,
        reserved_quantity=row.reserved_quantity,
        quantum=row.quantum,
    )


def risk_row(decision: RiskDecision) -> RiskDecisionRow:
    return RiskDecisionRow(
        decision_id=decision.decision_id,
        portfolio_target_id=decision.portfolio_target_id,
        account_id=decision.account_id,
        account_aggregate_sequence=decision.account_aggregate_sequence,
        portfolio_sequence=decision.portfolio_sequence,
        approved=decision.approved,
        decision_hash=decision.decision_hash,
        input_target_hash=decision.input_target_hash,
        authorized_target_hash=decision.authorized_target_hash,
        policy_hash=decision.policy_hash,
        payload=decision.model_dump(mode="json"),
        decided_at=decision.decided_at,
        expires_at=decision.expires_at,
    )


def risk_matches_target(decision: RiskDecision, target: PortfolioTargetRow) -> bool:
    return (
        decision.account_id == target.account_id
        and decision.input_target_hash == target.calculation_hash
        and decision.account_aggregate_sequence == target.account_aggregate_sequence
        and decision.portfolio_sequence == target.portfolio_sequence
    )


def reservation_row(reservation: AccountReservation) -> AccountReservationRow:
    return AccountReservationRow(
        reservation_id=reservation.reservation_id,
        order_intent_id=reservation.order_intent_id,
        account_id=reservation.account_id,
        instrument_id=reservation.instrument_id,
        kind=reservation.kind.value,
        commodity=reservation.commodity,
        amount=reservation.amount,
        remaining_amount=reservation.remaining_amount,
        quantum=reservation.quantum,
        risk_decision_id=reservation.risk_decision_id,
        risk_decision_hash=reservation.risk_decision_hash,
        portfolio_target_id=reservation.portfolio_target_id,
        authorized_target_hash=reservation.authorized_target_hash,
        risk_account_aggregate_sequence=reservation.risk_account_aggregate_sequence,
        account_aggregate_sequence=reservation.account_aggregate_sequence,
        portfolio_sequence=reservation.portfolio_sequence,
        state=reservation.state.value,
        created_at=reservation.created_at,
        updated_at=reservation.updated_at,
        expires_at=reservation.expires_at,
        event_sequence=reservation.event_sequence,
        previous_event_hash=reservation.previous_event_hash,
        event_hash=reservation.event_hash,
    )


def reservation_from_row(row: AccountReservationRow) -> AccountReservation:
    return AccountReservation(
        reservation_id=row.reservation_id,
        order_intent_id=row.order_intent_id,
        account_id=row.account_id,
        instrument_id=row.instrument_id,
        kind=ReservationKind(row.kind),
        commodity=row.commodity,
        amount=row.amount,
        remaining_amount=row.remaining_amount,
        quantum=row.quantum,
        risk_decision_id=row.risk_decision_id,
        risk_decision_hash=row.risk_decision_hash,
        portfolio_target_id=row.portfolio_target_id,
        authorized_target_hash=row.authorized_target_hash,
        risk_account_aggregate_sequence=row.risk_account_aggregate_sequence,
        account_aggregate_sequence=row.account_aggregate_sequence,
        portfolio_sequence=row.portfolio_sequence,
        state=ReservationState(row.state),
        created_at=row.created_at,
        updated_at=row.updated_at,
        expires_at=row.expires_at,
        event_sequence=row.event_sequence,
        previous_event_hash=row.previous_event_hash,
        event_hash=row.event_hash,
    )


def reservation_event_row(event: ReservationEvent) -> ReservationEventRow:
    return ReservationEventRow(
        event_id=event.event_id,
        reservation_id=event.reservation_id,
        sequence=event.sequence,
        event_type=event.event_type.value,
        from_state=event.from_state.value if event.from_state is not None else None,
        to_state=event.to_state.value,
        amount=event.amount,
        remaining_amount=event.remaining_amount,
        occurred_at=event.occurred_at,
        reason=event.reason,
        previous_event_hash=event.previous_event_hash,
        event_hash=event.event_hash,
    )


def reservation_event_from_row(row: ReservationEventRow) -> ReservationEvent:
    return ReservationEvent(
        event_id=row.event_id,
        reservation_id=row.reservation_id,
        sequence=row.sequence,
        event_type=ReservationEventType(row.event_type),
        from_state=ReservationState(row.from_state) if row.from_state else None,
        to_state=ReservationState(row.to_state),
        amount=row.amount,
        remaining_amount=row.remaining_amount,
        occurred_at=row.occurred_at,
        reason=row.reason,
        previous_event_hash=row.previous_event_hash,
        event_hash=row.event_hash,
    )


def order_intent_row(intent: OrderIntent) -> OrderIntentRow:
    return OrderIntentRow(
        intent_id=intent.intent_id,
        run_id=intent.run_id,
        account_id=intent.account_id,
        instrument_id=intent.instrument_id,
        reservation_id=intent.reservation_id,
        risk_decision_id=intent.risk_decision_id,
        portfolio_target_id=intent.portfolio_target_id,
        account_aggregate_sequence=intent.account_aggregate_sequence,
        portfolio_sequence=intent.portfolio_sequence,
        idempotency_key=intent.idempotency_key,
        intent_hash=intent.intent_hash,
        payload=intent.model_dump(mode="json"),
        valid_from=intent.valid_from,
        valid_until=intent.valid_until,
        created_at=intent.created_at,
    )


def order_event_row(event: OrderEvent) -> OrderEventRow:
    return OrderEventRow(
        event_id=event.event_id,
        order_intent_id=event.order_intent_id,
        sequence=event.sequence,
        from_status=event.from_status.value,
        to_status=event.to_status.value,
        cumulative_filled_quantity=event.cumulative_filled_quantity,
        remaining_quantity=event.remaining_quantity,
        occurred_at=event.occurred_at,
        reason=event.reason,
        previous_event_hash=event.previous_event_hash,
        event_hash=event.event_hash,
    )


def order_event_from_row(row: OrderEventRow) -> OrderEvent:
    return OrderEvent(
        event_id=row.event_id,
        order_intent_id=row.order_intent_id,
        sequence=row.sequence,
        from_status=OrderStatus(row.from_status),
        to_status=OrderStatus(row.to_status),
        cumulative_filled_quantity=row.cumulative_filled_quantity,
        remaining_quantity=row.remaining_quantity,
        occurred_at=row.occurred_at,
        reason=row.reason,
        previous_event_hash=row.previous_event_hash,
        event_hash=row.event_hash,
    )


def fill_row(fill: Fill) -> PaperFillRow:
    return PaperFillRow(
        fill_id=fill.fill_id,
        command_id=fill.command_id,
        order_intent_id=fill.order_intent_id,
        account_id=fill.account_id,
        instrument_id=fill.instrument_id,
        side=fill.side.value,
        quantity=fill.quantity,
        quantity_quantum=fill.quantity_quantum,
        price=fill.price,
        price_quantum=fill.price_quantum,
        fee_currency=fill.fee_currency,
        fees=fill.fees,
        fee_quantum=fill.fee_quantum,
        slippage=fill.slippage,
        occurred_at=fill.occurred_at,
        payload=fill.model_dump(mode="json"),
    )


def fill_matches_intent(fill: Fill, intent: OrderIntent) -> bool:
    return (
        fill.account_id == intent.account_id
        and fill.instrument_id == intent.instrument_id
        and fill.side is intent.side
        and fill.quantity_quantum == intent.quantity_quantum
        and fill.quantity <= intent.quantity
    )


def journal_row(transaction: JournalTransaction) -> JournalTransactionRow:
    return JournalTransactionRow(
        transaction_id=transaction.transaction_id,
        account_id=transaction.account_id,
        sequence=transaction.sequence,
        occurred_at=transaction.occurred_at,
        previous_hash=transaction.previous_hash,
        source_order_intent_id=transaction.source_order_intent_id,
        source_fill_id=transaction.source_fill_id,
        posting_count=len(transaction.postings),
        transaction_hash=transaction.transaction_hash,
    )


def posting_row(
    transaction_id: UUID, index: int, posting: JournalPosting
) -> JournalPostingRow:
    return JournalPostingRow(
        posting_id=posting.posting_id,
        transaction_id=transaction_id,
        posting_index=index,
        ledger_account=posting.ledger_account,
        commodity=posting.commodity,
        side=posting.side.value,
        amount=posting.amount,
        quantum=posting.quantum,
        memo=posting.memo,
    )


def posting_from_row(row: JournalPostingRow) -> JournalPosting:
    return JournalPosting(
        posting_id=row.posting_id,
        ledger_account=row.ledger_account,
        commodity=row.commodity,
        side=JournalSide(row.side),
        amount=row.amount,
        quantum=row.quantum,
        memo=row.memo,
    )


def new_account_event(
    *,
    account_id: str,
    sequence: int,
    event_type: str,
    aggregate_ref_type: str,
    aggregate_ref_id: UUID,
    occurred_at: datetime,
    previous_hash: str | None,
) -> PaperAccountEvent:
    identity = {
        "account_id": account_id,
        "sequence": sequence,
        "event_type": event_type,
        "aggregate_ref_type": aggregate_ref_type,
        "aggregate_ref_id": str(aggregate_ref_id),
        "occurred_at": occurred_at.isoformat().replace("+00:00", "Z"),
        "previous_hash": previous_hash,
    }
    event_id = uuid5(NAMESPACE_URL, stable_payload_hash(identity))
    payload = identity | {"event_id": str(event_id)}
    return PaperAccountEvent(
        event_id=event_id,
        account_id=account_id,
        sequence=sequence,
        event_type=event_type,
        aggregate_ref_type=aggregate_ref_type,
        aggregate_ref_id=aggregate_ref_id,
        occurred_at=occurred_at,
        previous_hash=previous_hash,
        event_hash=stable_payload_hash(payload),
    )


def account_event_row(event: PaperAccountEvent) -> PaperAccountEventRow:
    return PaperAccountEventRow(
        event_id=event.event_id,
        account_id=event.account_id,
        sequence=event.sequence,
        event_type=event.event_type,
        aggregate_ref_type=event.aggregate_ref_type,
        aggregate_ref_id=event.aggregate_ref_id,
        occurred_at=event.occurred_at,
        previous_hash=event.previous_hash,
        event_hash=event.event_hash,
    )


def account_event_from_row(row: PaperAccountEventRow) -> PaperAccountEvent:
    return PaperAccountEvent(
        event_id=row.event_id,
        account_id=row.account_id,
        sequence=row.sequence,
        event_type=row.event_type,
        aggregate_ref_type=row.aggregate_ref_type,
        aggregate_ref_id=row.aggregate_ref_id,
        occurred_at=row.occurred_at,
        previous_hash=row.previous_hash,
        event_hash=row.event_hash,
    )


def account_matches_snapshot(
    state: PaperAccountState,
    snapshot: AccountPortfolioSnapshot,
    base_currency: str,
) -> bool:
    return (
        state.base_currency == base_currency
        and state.account_aggregate_sequence == snapshot.account_aggregate_sequence
        and state.portfolio_sequence == snapshot.portfolio_sequence
        and state.ledger_sequence == snapshot.ledger_sequence
        and state.ledger_hash == snapshot.ledger_hash
        and state.cash == snapshot.cash
        and state.positions == snapshot.positions
    )
