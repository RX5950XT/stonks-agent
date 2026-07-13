"""PostgreSQL persistence for the canonical paper-trading aggregate."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from stonks_agent.adapters.postgres.models import (
    AccountReservationRow,
    JournalPostingRow,
    JournalTransactionRow,
    OrderEventRow,
    OrderIntentRow,
    PaperAccountEventRow,
    PaperAccountRow,
    PaperCashProjectionRow,
    PaperFillRow,
    PaperPositionProjectionRow,
    PortfolioTargetRow,
    ReservationEventRow,
    RiskDecisionRow,
)
from stonks_agent.adapters.postgres.trading_mapping import (
    account_event_from_row,
    account_event_row,
    account_matches_snapshot,
    cash_from_row,
    cash_row,
    fill_matches_intent,
    fill_row,
    journal_row,
    new_account_event,
    order_event_from_row,
    order_event_row,
    order_intent_row,
    position_from_row,
    position_row,
    posting_from_row,
    posting_row,
    reservation_event_from_row,
    reservation_event_row,
    reservation_from_row,
    reservation_row,
    risk_matches_target,
    risk_row,
)
from stonks_agent.adapters.postgres.trading_reservation_batch import (
    batch_identity_is_valid,
    existing_reservation_order_batch,
)
from stonks_agent.domain._trading import TradingModel
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.fills import Fill
from stonks_agent.domain.journal import JournalTransaction, verify_journal_chain
from stonks_agent.domain.orders import OrderEvent, OrderIntent
from stonks_agent.domain.portfolio import (
    AccountPortfolioSnapshot,
    PaperAccountState,
    PortfolioTarget,
)
from stonks_agent.domain.reservations import (
    AccountReservation,
    ReservationKind,
    ReservationMutation,
)
from stonks_agent.domain.risk import RiskDecision
from stonks_agent.domain.trading_persistence import (
    ReservationOrderBatchRecord,
    ReservationOrderItem,
    ReservationOrderRecord,
)

T = TypeVar("T")


class _MutationRejected(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class PostgresTradingRepository:
    """Flush-only repository; its owning unit of work controls commit/rollback."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def register_account(
        self,
        snapshot: AccountPortfolioSnapshot,
        *,
        base_currency: str,
    ) -> Result[PaperAccountState]:
        if (
            snapshot.account_aggregate_sequence != 0
            or snapshot.portfolio_sequence != 0
            or snapshot.ledger_sequence != 0
            or snapshot.ledger_hash is not None
            or snapshot.pending_order_ids
        ):
            return _failure(
                ErrorCode.INVALID_INPUT, "Paper account must start at genesis"
            )
        existing = self._session.get(PaperAccountRow, snapshot.account_id)
        if existing is not None:
            state = self.get_account(snapshot.account_id)
            if isinstance(state, Failure):
                return state
            if account_matches_snapshot(state.value, snapshot, base_currency):
                return state
            return _failure(ErrorCode.CONFLICT, "Paper account identity already exists")

        def mutate() -> PaperAccountState:
            row = PaperAccountRow(
                account_id=snapshot.account_id,
                base_currency=base_currency,
                aggregate_sequence=0,
                portfolio_sequence=0,
                ledger_sequence=0,
                ledger_hash=None,
                created_at=snapshot.as_of,
                updated_at=snapshot.as_of,
            )
            self._session.add(row)
            self._session.flush()
            self._session.refresh(row)
            for cash_balance in snapshot.cash:
                self._session.add(cash_row(snapshot, cash_balance))
            for position_balance in snapshot.positions:
                self._session.add(position_row(snapshot, position_balance))
            self._session.flush()
            state = self.get_account(snapshot.account_id)
            if isinstance(state, Failure):
                raise _MutationRejected(state.error.code, state.error.message)
            return state.value

        return self._mutation(mutate, "Paper account registration conflicted")

    def get_account(self, account_id: str) -> Result[PaperAccountState]:
        row = self._session.get(PaperAccountRow, account_id)
        if row is None:
            return _failure(ErrorCode.NOT_FOUND, "Paper account was not found")
        cash_rows = self._session.scalars(
            select(PaperCashProjectionRow)
            .where(PaperCashProjectionRow.account_id == account_id)
            .order_by(PaperCashProjectionRow.currency)
        ).all()
        position_rows = self._session.scalars(
            select(PaperPositionProjectionRow)
            .where(PaperPositionProjectionRow.account_id == account_id)
            .order_by(PaperPositionProjectionRow.instrument_id)
        ).all()
        event_rows = self._session.scalars(
            select(PaperAccountEventRow)
            .where(PaperAccountEventRow.account_id == account_id)
            .order_by(PaperAccountEventRow.sequence)
        ).all()
        try:
            return Success(
                PaperAccountState(
                    account_id=row.account_id,
                    base_currency=row.base_currency,
                    account_aggregate_sequence=row.aggregate_sequence,
                    portfolio_sequence=row.portfolio_sequence,
                    ledger_sequence=row.ledger_sequence,
                    ledger_hash=row.ledger_hash,
                    cash=tuple(cash_from_row(item) for item in cash_rows),
                    positions=tuple(position_from_row(item) for item in position_rows),
                    events=tuple(account_event_from_row(item) for item in event_rows),
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
            )
        except ValueError:
            return _failure(ErrorCode.CONFLICT, "Paper account integrity check failed")

    def save_target(self, target: PortfolioTarget) -> Result[PortfolioTarget]:
        existing = self._session.get(PortfolioTargetRow, target.target_id)
        if existing is not None:
            return _same_payload(existing.payload, target, "Portfolio target")

        def mutate() -> PortfolioTarget:
            account = self._session.get(PaperAccountRow, target.account_id)
            if account is None:
                raise _MutationRejected(
                    ErrorCode.NOT_FOUND, "Paper account was not found"
                )
            self._session.add(
                PortfolioTargetRow(
                    target_id=target.target_id,
                    account_id=target.account_id,
                    portfolio_snapshot_id=target.portfolio_snapshot_id,
                    account_aggregate_sequence=target.account_aggregate_sequence,
                    portfolio_sequence=target.portfolio_sequence,
                    calculation_hash=target.calculation_hash,
                    policy_hash=target.policy_hash,
                    payload=target.model_dump(mode="json"),
                    created_at=target.as_of,
                )
            )
            self._session.flush()
            return target

        return self._mutation(mutate, "Portfolio target registration conflicted")

    def save_risk_decision(self, decision: RiskDecision) -> Result[RiskDecision]:
        existing = self._session.get(RiskDecisionRow, decision.decision_id)
        if existing is not None:
            return _same_payload(existing.payload, decision, "Risk decision")

        def mutate() -> RiskDecision:
            target = self._session.get(PortfolioTargetRow, decision.portfolio_target_id)
            if target is None:
                raise _MutationRejected(
                    ErrorCode.NOT_FOUND, "Portfolio target was not found"
                )
            if not risk_matches_target(decision, target):
                raise _MutationRejected(
                    ErrorCode.CONFLICT, "Risk target binding mismatch"
                )
            self._session.add(risk_row(decision))
            self._session.flush()
            return decision

        return self._mutation(mutate, "Risk decision registration conflicted")

    def create_reservation_order(
        self,
        mutation: ReservationMutation,
        intent: OrderIntent,
    ) -> Result[ReservationOrderRecord]:
        existing = self._session.scalar(
            select(OrderIntentRow).where(
                OrderIntentRow.account_id == intent.account_id,
                OrderIntentRow.idempotency_key == intent.idempotency_key,
            )
        )
        if existing is not None:
            if existing.intent_hash != intent.intent_hash:
                return _failure(ErrorCode.CONFLICT, "Order idempotency payload changed")
            return self._reservation_order_from_row(existing)

        def mutate() -> ReservationOrderRecord:
            reservation = mutation.reservation
            self._validate_reservation_order_inputs(mutation, intent)
            account = self._advance_account(
                intent.account_id,
                expected_sequence=reservation.risk_account_aggregate_sequence,
                portfolio_sequence=reservation.portfolio_sequence,
            )
            self._advance_projection_sequences(
                intent.account_id,
                expected_sequence=reservation.risk_account_aggregate_sequence,
                new_sequence=reservation.account_aggregate_sequence,
            )
            self._reserve_projection(reservation)
            account_event = new_account_event(
                account_id=intent.account_id,
                sequence=account.aggregate_sequence,
                event_type="reservation_order.created",
                aggregate_ref_type="reservation_order",
                aggregate_ref_id=intent.intent_id,
                occurred_at=account.updated_at,
                previous_hash=self._previous_account_hash(intent.account_id),
            )
            self._session.add_all(
                [
                    reservation_row(reservation),
                    reservation_event_row(mutation.event),
                    order_intent_row(intent),
                    account_event_row(account_event),
                ]
            )
            self._session.flush()
            return ReservationOrderRecord(
                reservation=reservation,
                reservation_event=mutation.event,
                order_intent=intent,
                account_event=account_event,
            )

        return self._mutation(mutate, "Reservation/order mutation conflicted")

    def create_reservation_orders(
        self,
        pairs: tuple[tuple[ReservationMutation, OrderIntent], ...],
    ) -> Result[ReservationOrderBatchRecord]:
        if not pairs:
            return _failure(ErrorCode.INVALID_INPUT, "Reservation/order batch is empty")
        ordered = tuple(sorted(pairs, key=lambda item: str(item[1].intent_id)))
        replay = existing_reservation_order_batch(self._session, ordered)
        if replay is not None:
            return replay

        def mutate() -> ReservationOrderBatchRecord:
            for mutation, intent in ordered:
                self._validate_reservation_order_inputs(mutation, intent)
            reservations = tuple(item[0].reservation for item in ordered)
            intents = tuple(item[1] for item in ordered)
            if not batch_identity_is_valid(reservations, intents):
                raise _MutationRejected(
                    ErrorCode.CONFLICT,
                    "Reservation/order batch identity is inconsistent",
                )
            first = reservations[0]
            account = self._advance_account(
                first.account_id,
                expected_sequence=first.risk_account_aggregate_sequence,
                portfolio_sequence=first.portfolio_sequence,
            )
            self._advance_projection_sequences(
                first.account_id,
                expected_sequence=first.risk_account_aggregate_sequence,
                new_sequence=first.account_aggregate_sequence,
            )
            for reservation in reservations:
                self._reserve_projection(reservation)
            account_event = new_account_event(
                account_id=first.account_id,
                sequence=account.aggregate_sequence,
                event_type="reservation_orders.created",
                aggregate_ref_type="reservation_orders",
                aggregate_ref_id=first.risk_decision_id,
                occurred_at=account.updated_at,
                previous_hash=self._previous_account_hash(first.account_id),
            )
            items = tuple(
                ReservationOrderItem(
                    reservation=mutation.reservation,
                    reservation_event=mutation.event,
                    order_intent=intent,
                )
                for mutation, intent in ordered
            )
            rows: list[object] = [account_event_row(account_event)]
            for mutation, intent in ordered:
                rows.extend(
                    (
                        reservation_row(mutation.reservation),
                        reservation_event_row(mutation.event),
                        order_intent_row(intent),
                    )
                )
            self._session.add_all(rows)
            self._session.flush()
            return ReservationOrderBatchRecord(items=items, account_event=account_event)

        return self._mutation(mutate, "Reservation/order batch mutation conflicted")

    def append_order_event(self, event: OrderEvent) -> Result[OrderEvent]:
        existing = self._session.get(OrderEventRow, event.event_id)
        if existing is not None:
            try:
                persisted = order_event_from_row(existing)
            except ValueError:
                return _failure(
                    ErrorCode.CONFLICT, "Order event integrity check failed"
                )
            if persisted == event:
                return Success(persisted)
            return _failure(ErrorCode.CONFLICT, "Order event id already exists")

        def mutate() -> OrderEvent:
            intent_row = self._session.get(OrderIntentRow, event.order_intent_id)
            if intent_row is None:
                raise _MutationRejected(
                    ErrorCode.NOT_FOUND, "Order intent was not found"
                )
            intent = OrderIntent.model_validate(intent_row.payload)
            if (
                event.cumulative_filled_quantity + event.remaining_quantity
                != intent.quantity
                or event.cumulative_filled_quantity > intent.quantity
            ):
                raise _MutationRejected(
                    ErrorCode.CONFLICT, "Order event quantities mismatch"
                )
            self._session.add(order_event_row(event))
            self._session.flush()
            return event

        return self._mutation(mutate, "Order event append conflicted")

    def list_order_events(self, intent_id: UUID) -> Result[tuple[OrderEvent, ...]]:
        rows = self._session.scalars(
            select(OrderEventRow)
            .where(OrderEventRow.order_intent_id == intent_id)
            .order_by(OrderEventRow.sequence)
        ).all()
        try:
            events = tuple(order_event_from_row(row) for row in rows)
        except ValueError:
            return _failure(ErrorCode.CONFLICT, "Order event integrity check failed")
        previous_hash: str | None = None
        for sequence, event in enumerate(events, start=1):
            if event.sequence != sequence or event.previous_event_hash != previous_hash:
                return _failure(ErrorCode.CONFLICT, "Order event chain is invalid")
            previous_hash = event.event_hash
        return Success(events)

    def save_fill(self, fill: Fill) -> Result[Fill]:
        existing = self._session.get(PaperFillRow, fill.fill_id)
        if existing is not None:
            return _same_payload(existing.payload, fill, "Paper fill")

        def mutate() -> Fill:
            intent_row = self._session.get(OrderIntentRow, fill.order_intent_id)
            if intent_row is None:
                raise _MutationRejected(
                    ErrorCode.NOT_FOUND, "Order intent was not found"
                )
            intent = OrderIntent.model_validate(intent_row.payload)
            if not fill_matches_intent(fill, intent):
                raise _MutationRejected(
                    ErrorCode.CONFLICT, "Fill order binding mismatch"
                )
            self._session.add(fill_row(fill))
            self._session.flush()
            return fill

        return self._mutation(mutate, "Paper fill append conflicted")

    def append_journal(
        self,
        transaction: JournalTransaction,
        *,
        expected_account_sequence: int,
    ) -> Result[JournalTransaction]:
        existing = self._session.get(JournalTransactionRow, transaction.transaction_id)
        if existing is not None:
            persisted = self._journal_from_row(existing)
            if isinstance(persisted, Success) and persisted.value == transaction:
                return persisted
            return _failure(ErrorCode.CONFLICT, "Journal transaction id already exists")

        def mutate() -> JournalTransaction:
            self._validate_journal_sources(transaction)
            account = self._advance_account(
                transaction.account_id,
                expected_sequence=expected_account_sequence,
                ledger_sequence=transaction.sequence,
                ledger_hash=transaction.transaction_hash,
            )
            account_event = new_account_event(
                account_id=transaction.account_id,
                sequence=account.aggregate_sequence,
                event_type="journal.posted",
                aggregate_ref_type="journal_transaction",
                aggregate_ref_id=transaction.transaction_id,
                occurred_at=account.updated_at,
                previous_hash=self._previous_account_hash(transaction.account_id),
            )
            self._session.add(journal_row(transaction))
            self._session.flush()
            self._session.add_all(
                posting_row(transaction.transaction_id, index, posting)
                for index, posting in enumerate(transaction.postings)
            )
            self._session.add(account_event_row(account_event))
            self._session.flush()
            return transaction

        return self._mutation(mutate, "Journal append conflicted")

    def list_journal(self, account_id: str) -> Result[tuple[JournalTransaction, ...]]:
        rows = self._session.scalars(
            select(JournalTransactionRow)
            .where(JournalTransactionRow.account_id == account_id)
            .order_by(JournalTransactionRow.sequence)
        ).all()
        transactions: list[JournalTransaction] = []
        for row in rows:
            parsed = self._journal_from_row(row)
            if isinstance(parsed, Failure):
                return parsed
            transactions.append(parsed.value)
        result = tuple(transactions)
        if result and not verify_journal_chain(result):
            return _failure(ErrorCode.CONFLICT, "Journal chain is invalid")
        return Success(result)

    def _validate_reservation_order_inputs(
        self, mutation: ReservationMutation, intent: OrderIntent
    ) -> None:
        reservation = mutation.reservation
        if (
            reservation.order_intent_id != intent.intent_id
            or reservation.reservation_id != intent.reservation_id
            or reservation.event_hash != intent.reservation_hash
            or reservation.risk_decision_id != intent.risk_decision_id
            or reservation.portfolio_target_id != intent.portfolio_target_id
            or reservation.account_id != intent.account_id
            or reservation.account_aggregate_sequence
            != intent.account_aggregate_sequence
        ):
            raise _MutationRejected(
                ErrorCode.CONFLICT, "Reservation/order binding mismatch"
            )
        risk = self._session.get(RiskDecisionRow, reservation.risk_decision_id)
        target = self._session.get(PortfolioTargetRow, reservation.portfolio_target_id)
        if risk is None or target is None:
            raise _MutationRejected(
                ErrorCode.NOT_FOUND, "Trading authorization was not found"
            )
        if (
            risk.decision_hash != reservation.risk_decision_hash
            or target.calculation_hash != reservation.authorized_target_hash
            or risk.authorized_target_hash != reservation.authorized_target_hash
            or not risk.approved
        ):
            raise _MutationRejected(
                ErrorCode.CONFLICT, "Trading authorization mismatch"
            )

    def _advance_account(
        self,
        account_id: str,
        *,
        expected_sequence: int,
        portfolio_sequence: int | None = None,
        ledger_sequence: int | None = None,
        ledger_hash: str | None = None,
    ) -> PaperAccountRow:
        values: dict[str, object] = {"aggregate_sequence": expected_sequence + 1}
        predicates = [
            PaperAccountRow.account_id == account_id,
            PaperAccountRow.aggregate_sequence == expected_sequence,
        ]
        if portfolio_sequence is not None:
            predicates.append(PaperAccountRow.portfolio_sequence == portfolio_sequence)
        if ledger_sequence is not None:
            predicates.append(PaperAccountRow.ledger_sequence == ledger_sequence - 1)
            values.update(ledger_sequence=ledger_sequence, ledger_hash=ledger_hash)
        row = self._session.scalar(
            update(PaperAccountRow)
            .where(*predicates)
            .values(**values)
            .returning(PaperAccountRow)
        )
        if row is None:
            raise _MutationRejected(ErrorCode.CONFLICT, "Paper account CAS failed")
        return row

    def _advance_projection_sequences(
        self,
        account_id: str,
        *,
        expected_sequence: int,
        new_sequence: int,
    ) -> None:
        cash = self._session.scalars(
            select(PaperCashProjectionRow)
            .where(PaperCashProjectionRow.account_id == account_id)
            .with_for_update()
        ).all()
        positions = self._session.scalars(
            select(PaperPositionProjectionRow)
            .where(PaperPositionProjectionRow.account_id == account_id)
            .with_for_update()
        ).all()
        if any(item.updated_sequence != expected_sequence for item in cash) or any(
            item.updated_sequence != expected_sequence for item in positions
        ):
            raise _MutationRejected(
                ErrorCode.CONFLICT,
                "Paper account projection sequence is stale",
            )
        for cash_item in cash:
            cash_item.updated_sequence = new_sequence
        for position_item in positions:
            position_item.updated_sequence = new_sequence
        self._session.flush()

    def _reserve_projection(self, reservation: AccountReservation) -> None:
        if reservation.kind is ReservationKind.CASH:
            cash = self._session.scalar(
                update(PaperCashProjectionRow)
                .where(
                    PaperCashProjectionRow.account_id == reservation.account_id,
                    PaperCashProjectionRow.currency == reservation.commodity,
                    PaperCashProjectionRow.quantum == reservation.quantum,
                    PaperCashProjectionRow.updated_sequence
                    == reservation.account_aggregate_sequence,
                    PaperCashProjectionRow.reserved_amount + reservation.amount
                    <= PaperCashProjectionRow.settled_amount,
                )
                .values(
                    reserved_amount=PaperCashProjectionRow.reserved_amount
                    + reservation.amount,
                    updated_sequence=reservation.account_aggregate_sequence,
                )
                .returning(PaperCashProjectionRow)
            )
            if cash is None:
                raise _MutationRejected(
                    ErrorCode.CONFLICT, "Available balance is insufficient"
                )
            return
        position = self._session.scalar(
            update(PaperPositionProjectionRow)
            .where(
                PaperPositionProjectionRow.account_id == reservation.account_id,
                PaperPositionProjectionRow.instrument_id == reservation.instrument_id,
                PaperPositionProjectionRow.quantum == reservation.quantum,
                PaperPositionProjectionRow.updated_sequence
                == reservation.account_aggregate_sequence,
                PaperPositionProjectionRow.reserved_quantity + reservation.amount
                <= PaperPositionProjectionRow.sellable_quantity,
            )
            .values(
                reserved_quantity=PaperPositionProjectionRow.reserved_quantity
                + reservation.amount,
                updated_sequence=reservation.account_aggregate_sequence,
            )
            .returning(PaperPositionProjectionRow)
        )
        if position is None:
            raise _MutationRejected(
                ErrorCode.CONFLICT, "Available balance is insufficient"
            )

    def _previous_account_hash(self, account_id: str) -> str | None:
        return self._session.scalar(
            select(PaperAccountEventRow.event_hash)
            .where(PaperAccountEventRow.account_id == account_id)
            .order_by(PaperAccountEventRow.sequence.desc())
            .limit(1)
        )

    def _reservation_order_from_row(
        self, row: OrderIntentRow
    ) -> Result[ReservationOrderRecord]:
        reservation_row = self._session.get(AccountReservationRow, row.reservation_id)
        reservation_event = self._session.scalar(
            select(ReservationEventRow).where(
                ReservationEventRow.reservation_id == row.reservation_id,
                ReservationEventRow.sequence == 1,
            )
        )
        account_event = self._session.scalar(
            select(PaperAccountEventRow).where(
                PaperAccountEventRow.account_id == row.account_id,
                PaperAccountEventRow.aggregate_ref_type == "reservation_order",
                PaperAccountEventRow.aggregate_ref_id == row.intent_id,
            )
        )
        if (
            reservation_row is None
            or reservation_event is None
            or account_event is None
        ):
            return _failure(
                ErrorCode.CONFLICT, "Reservation/order record is incomplete"
            )
        try:
            return Success(
                ReservationOrderRecord(
                    reservation=reservation_from_row(reservation_row),
                    reservation_event=reservation_event_from_row(reservation_event),
                    order_intent=OrderIntent.model_validate(row.payload),
                    account_event=account_event_from_row(account_event),
                )
            )
        except ValueError:
            return _failure(
                ErrorCode.CONFLICT, "Reservation/order integrity check failed"
            )

    def _validate_journal_sources(self, transaction: JournalTransaction) -> None:
        fill = self._session.get(PaperFillRow, transaction.source_fill_id)
        intent = self._session.get(OrderIntentRow, transaction.source_order_intent_id)
        if fill is None or intent is None:
            raise _MutationRejected(ErrorCode.NOT_FOUND, "Journal source was not found")
        if (
            fill.order_intent_id != transaction.source_order_intent_id
            or fill.account_id != transaction.account_id
            or intent.account_id != transaction.account_id
        ):
            raise _MutationRejected(
                ErrorCode.CONFLICT, "Journal source binding mismatch"
            )

    def _journal_from_row(
        self, row: JournalTransactionRow
    ) -> Result[JournalTransaction]:
        posting_rows = self._session.scalars(
            select(JournalPostingRow)
            .where(JournalPostingRow.transaction_id == row.transaction_id)
            .order_by(JournalPostingRow.posting_index)
        ).all()
        try:
            transaction = JournalTransaction(
                transaction_id=row.transaction_id,
                account_id=row.account_id,
                sequence=row.sequence,
                occurred_at=row.occurred_at,
                previous_hash=row.previous_hash,
                source_order_intent_id=row.source_order_intent_id,
                source_fill_id=row.source_fill_id,
                postings=tuple(posting_from_row(item) for item in posting_rows),
                transaction_hash=row.transaction_hash,
            )
        except ValueError:
            return _failure(ErrorCode.CONFLICT, "Journal integrity check failed")
        return Success(transaction)

    def _mutation(self, operation: Callable[[], T], conflict_message: str) -> Result[T]:
        try:
            with self._session.begin_nested():
                value = operation()
            return Success(value)
        except _MutationRejected as error:
            return _failure(error.code, error.message)
        except ValueError:
            return _failure(ErrorCode.CONFLICT, conflict_message)
        except IntegrityError:
            return _failure(ErrorCode.CONFLICT, conflict_message)
        except DBAPIError as error:
            code = (
                ErrorCode.CONFLICT
                if getattr(error.orig, "sqlstate", None)
                in {"23505", "23514", "40001", "55000"}
                else ErrorCode.INTERNAL_ERROR
            )
            return _failure(code, conflict_message)


def _same_payload[TModel: TradingModel](
    payload: dict[str, object], model: TModel, label: str
) -> Result[TModel]:
    model_type = type(model)
    try:
        persisted = model_type.model_validate(payload)
    except ValueError:
        return _failure(ErrorCode.CONFLICT, f"{label} integrity check failed")
    if persisted == model:
        return Success(model)
    return _failure(ErrorCode.CONFLICT, f"{label} id already exists")


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
