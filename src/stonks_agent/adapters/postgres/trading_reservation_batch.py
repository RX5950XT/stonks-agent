"""Read/validation helpers for atomic reservation and order batches."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from stonks_agent.adapters.postgres.models import (
    AccountReservationRow,
    OrderIntentRow,
    PaperAccountEventRow,
    ReservationEventRow,
)
from stonks_agent.adapters.postgres.trading_mapping import (
    account_event_from_row,
    reservation_event_from_row,
    reservation_from_row,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.orders import OrderIntent
from stonks_agent.domain.reservations import AccountReservation, ReservationMutation
from stonks_agent.domain.trading_persistence import (
    ReservationOrderBatchRecord,
    ReservationOrderItem,
)


def batch_identity_is_valid(
    reservations: tuple[AccountReservation, ...],
    intents: tuple[OrderIntent, ...],
) -> bool:
    identities = {
        (
            item.account_id,
            item.risk_decision_id,
            item.risk_decision_hash,
            item.portfolio_target_id,
            item.authorized_target_hash,
            item.risk_account_aggregate_sequence,
            item.account_aggregate_sequence,
            item.portfolio_sequence,
        )
        for item in reservations
    }
    return len(identities) == 1 and (
        len({item.reservation_id for item in reservations}) == len(reservations)
        and len({item.intent_id for item in intents}) == len(intents)
        and len({item.idempotency_key for item in intents}) == len(intents)
        and len({item.instrument_id for item in intents}) == len(intents)
    )


def existing_reservation_order_batch(
    session: Session,
    pairs: tuple[tuple[ReservationMutation, OrderIntent], ...],
) -> Result[ReservationOrderBatchRecord] | None:
    rows = tuple(
        session.scalar(
            select(OrderIntentRow).where(
                OrderIntentRow.account_id == intent.account_id,
                OrderIntentRow.idempotency_key == intent.idempotency_key,
            )
        )
        for _, intent in pairs
    )
    present = tuple(row is not None for row in rows)
    if not any(present):
        return None
    if not all(present):
        return _failure("Reservation/order batch is partial")
    persisted = tuple(row for row in rows if row is not None)
    if any(
        row.intent_hash != pair[1].intent_hash
        for row, pair in zip(persisted, pairs, strict=True)
    ):
        return _failure("Order idempotency payload changed")
    decision_id = pairs[0][0].reservation.risk_decision_id
    account_id = pairs[0][0].reservation.account_id
    event_row = session.scalar(
        select(PaperAccountEventRow).where(
            PaperAccountEventRow.account_id == account_id,
            PaperAccountEventRow.aggregate_ref_type == "reservation_orders",
            PaperAccountEventRow.aggregate_ref_id == decision_id,
        )
    )
    if event_row is None:
        return _failure("Reservation/order batch event is missing")
    try:
        items = tuple(_item_from_row(session, row) for row in persisted)
        return Success(
            ReservationOrderBatchRecord(
                items=items,
                account_event=account_event_from_row(event_row),
            )
        )
    except ValueError:
        return _failure("Reservation/order batch integrity check failed")


def _item_from_row(session: Session, row: OrderIntentRow) -> ReservationOrderItem:
    reservation = session.get(AccountReservationRow, row.reservation_id)
    event = session.scalar(
        select(ReservationEventRow).where(
            ReservationEventRow.reservation_id == row.reservation_id,
            ReservationEventRow.sequence == 1,
        )
    )
    if reservation is None or event is None:
        raise ValueError("reservation/order item is incomplete")
    return ReservationOrderItem(
        reservation=reservation_from_row(reservation),
        reservation_event=reservation_event_from_row(event),
        order_intent=OrderIntent.model_validate(row.payload),
    )


def _failure(message: str) -> Failure:
    return Failure(StructuredError(code=ErrorCode.CONFLICT, message=message))
