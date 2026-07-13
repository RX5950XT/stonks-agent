"""Atomic PostgreSQL persistence for reference paper execution outcomes."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from stonks_agent.adapters.postgres.models import (
    AccountReservationRow,
    OrderEventRow,
    OrderIntentRow,
    PaperAccountRow,
    PaperCashProjectionRow,
    PaperExecutionReceiptRow,
    PaperFillRow,
    PaperPositionProjectionRow,
)
from stonks_agent.adapters.postgres.trading_mapping import (
    fill_row,
    order_event_from_row,
    order_event_row,
    reservation_event_row,
    reservation_from_row,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.execution_model import PaperExecutionOutcome
from stonks_agent.domain.fills import Fill
from stonks_agent.domain.orders import ExecutionCommand, OrderEvent, OrderIntent
from stonks_agent.domain.reservations import AccountReservation, ReservationKind
from stonks_agent.domain.trading_persistence import PaperExecutionRecord


class _Rejected(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def get_order_by_idempotency(
    session: Session,
    *,
    account_id: str,
    idempotency_key: str,
) -> Result[OrderIntent]:
    row = session.scalar(
        select(OrderIntentRow).where(
            OrderIntentRow.account_id == account_id,
            OrderIntentRow.idempotency_key == idempotency_key,
        )
    )
    if row is None:
        return _failure(ErrorCode.NOT_FOUND, "Paper order was not found")
    try:
        intent = OrderIntent.model_validate(row.payload)
    except ValueError:
        return _failure(ErrorCode.CONFLICT, "Paper order integrity check failed")
    if (
        intent.account_id != row.account_id
        or intent.idempotency_key != row.idempotency_key
        or intent.intent_hash != row.intent_hash
    ):
        return _failure(ErrorCode.CONFLICT, "Paper order indexed identity changed")
    return Success(intent)


def get_reservation(
    session: Session,
    reservation_id: UUID,
) -> Result[AccountReservation]:
    row = session.get(AccountReservationRow, reservation_id)
    if row is None:
        return _failure(ErrorCode.NOT_FOUND, "Paper reservation was not found")
    try:
        return Success(reservation_from_row(row))
    except ValueError:
        return _failure(ErrorCode.CONFLICT, "Paper reservation integrity check failed")


def list_fills(session: Session, intent_id: UUID) -> Result[tuple[Fill, ...]]:
    rows = session.scalars(
        select(PaperFillRow)
        .where(PaperFillRow.order_intent_id == intent_id)
        .order_by(PaperFillRow.fill_id)
    ).all()
    try:
        fills = tuple(Fill.model_validate(row.payload) for row in rows)
    except ValueError:
        return _failure(ErrorCode.CONFLICT, "Paper fill integrity check failed")
    if any(
        not _fill_matches_row(fill, row) for fill, row in zip(fills, rows, strict=True)
    ):
        return _failure(ErrorCode.CONFLICT, "Paper fill indexed identity changed")
    return Success(fills)


def get_execution_record(
    session: Session,
    *,
    account_id: str,
    idempotency_key: str,
) -> Result[PaperExecutionRecord]:
    row = session.scalar(
        select(PaperExecutionReceiptRow).where(
            PaperExecutionReceiptRow.account_id == account_id,
            PaperExecutionReceiptRow.idempotency_key == idempotency_key,
        )
    )
    if row is None:
        return _failure(ErrorCode.NOT_FOUND, "Paper execution receipt was not found")
    return _record_from_row(row)


def apply_paper_execution(
    session: Session,
    command: ExecutionCommand,
    outcome: PaperExecutionOutcome,
    *,
    expected_account_sequence: int,
) -> Result[PaperExecutionRecord]:
    def operation() -> PaperExecutionRecord:
        intent = command.intent
        account = session.scalar(
            select(PaperAccountRow)
            .where(PaperAccountRow.account_id == intent.account_id)
            .with_for_update()
        )
        if account is None:
            raise _Rejected(ErrorCode.NOT_FOUND, "Paper account was not found")
        existing = session.scalar(
            select(PaperExecutionReceiptRow).where(
                PaperExecutionReceiptRow.account_id == intent.account_id,
                PaperExecutionReceiptRow.idempotency_key == intent.idempotency_key,
            )
        )
        if existing is not None:
            record = _required_record(existing)
            if record.command_hash != command.command_hash:
                raise _Rejected(
                    ErrorCode.CONFLICT, "Execution idempotency payload changed"
                )
            return record
        if account.aggregate_sequence != expected_account_sequence:
            raise _Rejected(ErrorCode.CONFLICT, "Execution account sequence is stale")
        order_row = session.get(OrderIntentRow, intent.intent_id)
        reservation_row = session.get(AccountReservationRow, intent.reservation_id)
        if order_row is None or reservation_row is None:
            raise _Rejected(ErrorCode.NOT_FOUND, "Execution authority was not found")
        persisted_intent = _required_intent(order_row)
        persisted_reservation = _required_reservation(reservation_row)
        _validate_identity(command, outcome, persisted_intent, persisted_reservation)
        prior_events = _required_events(session, intent.intent_id)
        prior_fills = _required_fills(session, intent.intent_id)
        new_events = _suffix(outcome.order_events, prior_events, "order event")
        new_fills = _suffix(outcome.receipt.fills, prior_fills, "paper fill")
        _apply_reservation(session, reservation_row, persisted_reservation, outcome)
        for event in new_events:
            session.add(order_event_row(event))
            session.flush()
        for fill in new_fills:
            session.add(fill_row(fill))
        record = PaperExecutionRecord(
            account_id=intent.account_id,
            idempotency_key=intent.idempotency_key,
            command_id=command.command_id,
            command_hash=command.command_hash,
            intent_hash=intent.intent_hash,
            outcome=outcome,
        )
        receipt = outcome.receipt
        session.add(
            PaperExecutionReceiptRow(
                receipt_id=receipt.receipt_id,
                account_id=record.account_id,
                idempotency_key=record.idempotency_key,
                command_id=record.command_id,
                command_hash=record.command_hash,
                order_intent_id=intent.intent_id,
                intent_hash=record.intent_hash,
                receipt_hash=receipt.receipt_hash,
                outcome_hash=outcome.outcome_hash,
                payload=record.model_dump(mode="json"),
                created_at=receipt.occurred_at,
            )
        )
        session.flush()
        return record

    return _mutation(session, operation)


def _apply_reservation(
    session: Session,
    row: AccountReservationRow,
    before: AccountReservation,
    outcome: PaperExecutionOutcome,
) -> None:
    mutations = outcome.reservation_mutations
    if not mutations:
        if outcome.final_reservation != before:
            raise _Rejected(ErrorCode.CONFLICT, "Reservation changed without event")
        return
    previous = before
    for mutation in mutations:
        current = mutation.reservation
        event = mutation.event
        valid = (
            current.reservation_id == before.reservation_id
            and event.sequence == previous.event_sequence + 1
            and event.previous_event_hash == previous.event_hash
            and current.previous_event_hash == previous.event_hash
        )
        if not valid:
            raise _Rejected(ErrorCode.CONFLICT, "Reservation event chain changed")
        session.add(reservation_event_row(event))
        session.flush()
        row.remaining_amount = current.remaining_amount
        row.state = current.state.value
        row.updated_at = current.updated_at
        row.event_sequence = current.event_sequence
        row.previous_event_hash = current.previous_event_hash
        row.event_hash = current.event_hash
        session.flush()
        previous = current
    if previous != outcome.final_reservation:
        raise _Rejected(ErrorCode.CONFLICT, "Reservation outcome is incomplete")
    decrease = before.remaining_amount - previous.remaining_amount
    if decrease < 0:
        raise _Rejected(ErrorCode.CONFLICT, "Reservation remaining amount increased")
    _release_projection(session, before, decrease)
    session.flush()


def _release_projection(
    session: Session,
    reservation: AccountReservation,
    decrease: Decimal,
) -> None:
    if decrease == 0:
        return
    updated: object | None
    if reservation.kind is ReservationKind.CASH:
        updated = session.scalar(
            update(PaperCashProjectionRow)
            .where(
                PaperCashProjectionRow.account_id == reservation.account_id,
                PaperCashProjectionRow.currency == reservation.commodity,
                PaperCashProjectionRow.updated_sequence
                == reservation.account_aggregate_sequence,
                PaperCashProjectionRow.reserved_amount >= decrease,
            )
            .values(reserved_amount=PaperCashProjectionRow.reserved_amount - decrease)
            .returning(PaperCashProjectionRow)
        )
    else:
        updated = session.scalar(
            update(PaperPositionProjectionRow)
            .where(
                PaperPositionProjectionRow.account_id == reservation.account_id,
                PaperPositionProjectionRow.instrument_id == reservation.instrument_id,
                PaperPositionProjectionRow.updated_sequence
                == reservation.account_aggregate_sequence,
                PaperPositionProjectionRow.reserved_quantity >= decrease,
            )
            .values(
                reserved_quantity=PaperPositionProjectionRow.reserved_quantity
                - decrease
            )
            .returning(PaperPositionProjectionRow)
        )
    if updated is None:
        raise _Rejected(ErrorCode.CONFLICT, "Reserved projection could not be released")


def _validate_identity(
    command: ExecutionCommand,
    outcome: PaperExecutionOutcome,
    intent: OrderIntent,
    reservation: AccountReservation,
) -> None:
    receipt = outcome.receipt
    valid = (
        command.intent == intent
        and command.command_hash == command.expected_command_hash()
        and reservation.reservation_id == intent.reservation_id
        and receipt.command_id == command.command_id
        and receipt.intent_hash == intent.intent_hash
        and outcome.final_reservation.reservation_id == reservation.reservation_id
    )
    if not valid:
        raise _Rejected(ErrorCode.CONFLICT, "Paper execution identity changed")


def _required_events(session: Session, intent_id: UUID) -> tuple[OrderEvent, ...]:
    rows = session.scalars(
        select(OrderEventRow)
        .where(OrderEventRow.order_intent_id == intent_id)
        .order_by(OrderEventRow.sequence)
    ).all()
    try:
        return tuple(order_event_from_row(row) for row in rows)
    except ValueError as error:
        raise _Rejected(
            ErrorCode.CONFLICT, "Order event integrity check failed"
        ) from error


def _required_fills(session: Session, intent_id: UUID) -> tuple[Fill, ...]:
    result = list_fills(session, intent_id)
    if isinstance(result, Failure):
        raise _Rejected(result.error.code, result.error.message)
    return result.value


def _required_intent(row: OrderIntentRow) -> OrderIntent:
    try:
        return OrderIntent.model_validate(row.payload)
    except ValueError as error:
        raise _Rejected(
            ErrorCode.CONFLICT, "Paper order integrity check failed"
        ) from error


def _required_reservation(row: AccountReservationRow) -> AccountReservation:
    try:
        return reservation_from_row(row)
    except ValueError as error:
        raise _Rejected(
            ErrorCode.CONFLICT, "Paper reservation integrity check failed"
        ) from error


def _required_record(row: PaperExecutionReceiptRow) -> PaperExecutionRecord:
    result = _record_from_row(row)
    if isinstance(result, Failure):
        raise _Rejected(result.error.code, result.error.message)
    return result.value


def _record_from_row(row: PaperExecutionReceiptRow) -> Result[PaperExecutionRecord]:
    try:
        record = PaperExecutionRecord.model_validate(row.payload)
    except ValueError:
        return _failure(ErrorCode.CONFLICT, "Paper execution integrity check failed")
    receipt = record.outcome.receipt
    matches = (
        record.account_id == row.account_id
        and record.idempotency_key == row.idempotency_key
        and record.command_id == row.command_id
        and record.command_hash == row.command_hash
        and record.intent_hash == row.intent_hash
        and receipt.receipt_id == row.receipt_id
        and receipt.receipt_hash == row.receipt_hash
        and record.outcome.outcome_hash == row.outcome_hash
    )
    if not matches:
        return _failure(ErrorCode.CONFLICT, "Paper execution indexed identity changed")
    return Success(record)


def _fill_matches_row(fill: Fill, row: PaperFillRow) -> bool:
    return (
        fill.fill_id == row.fill_id
        and fill.command_id == row.command_id
        and fill.order_intent_id == row.order_intent_id
        and fill.account_id == row.account_id
        and fill.instrument_id == row.instrument_id
    )


def _suffix[TItem](
    complete: tuple[TItem, ...],
    prefix: tuple[TItem, ...],
    label: str,
) -> tuple[TItem, ...]:
    if len(complete) < len(prefix) or complete[: len(prefix)] != prefix:
        raise _Rejected(ErrorCode.CONFLICT, f"Persisted {label} prefix changed")
    return complete[len(prefix) :]


def _mutation[T](
    session: Session,
    operation: Callable[[], T],
) -> Result[T]:
    try:
        with session.begin_nested():
            return Success(operation())
    except _Rejected as error:
        return _failure(error.code, error.message)
    except (IntegrityError, ValueError):
        return _failure(ErrorCode.CONFLICT, "Paper execution mutation conflicted")
    except DBAPIError as error:
        code = (
            ErrorCode.CONFLICT
            if getattr(error.orig, "sqlstate", None)
            in {"23505", "23514", "40001", "55000"}
            else ErrorCode.INTERNAL_ERROR
        )
        return _failure(code, "Paper execution persistence failed")


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
