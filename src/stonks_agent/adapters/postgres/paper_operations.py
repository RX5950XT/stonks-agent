"""PostgreSQL authority for audited paper kill-switch operations."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from stonks_agent.adapters.postgres.models import (
    AccountReservationRow,
    OrderEventRow,
    OrderIntentRow,
    PaperAccountRow,
    PaperCashProjectionRow,
    PaperKillSwitchRow,
    PaperOperatorActionRow,
    PaperOperatorAuditHeadRow,
    PaperPositionProjectionRow,
)
from stonks_agent.adapters.postgres.trading_mapping import (
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
from stonks_agent.domain.ledger import LedgerReconciliationReport
from stonks_agent.domain.operations import (
    ActivateKillSwitchCommand,
    KillSwitchScope,
    OperatorActionType,
    PaperKillSwitchState,
    PaperOperationRecord,
    PaperOperatorAction,
    PaperReconciliationResult,
    ReconcilePaperCommand,
    ResumePaperCommand,
    ResumePreparation,
    reconciliation_report_hash,
)
from stonks_agent.domain.orders import (
    OrderEvent,
    OrderIntent,
    OrderStatus,
    append_order_event,
)
from stonks_agent.domain.reservations import (
    AccountReservation,
    ReservationKind,
    ReservationState,
    expire_reservation,
    release_reservation,
)


class _Rejected(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class PostgresPaperOperationsRepository:
    """Flush-only operator repository; the core unit of work owns commit."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_kill_switch(
        self, scope: KillSwitchScope, account_id: str | None
    ) -> Result[PaperKillSwitchState]:
        try:
            row = self._switch_row(scope, account_id, lock=False)
            return Success(_state_from_row(row))
        except _Rejected as error:
            return _failure(error.code, error.message)

    def list_actions(
        self, *, after_sequence: int = 0
    ) -> Result[tuple[PaperOperatorAction, ...]]:
        if after_sequence < 0:
            return _failure(ErrorCode.INVALID_INPUT, "Operator sequence is invalid")
        try:
            actions = self._verified_actions()
        except _Rejected as error:
            return _failure(error.code, error.message)
        return Success(
            tuple(item for item in actions if item.sequence > after_sequence)
        )

    def activate(
        self, command: ActivateKillSwitchCommand, *, actor: str
    ) -> Result[PaperOperationRecord]:
        return self._mutation(
            lambda: self._activate(
                action_id=command.action_id,
                scope=command.scope,
                account_id=command.account_id,
                expected_version=command.expected_version,
                actor=actor,
                reason_code=command.reason_code,
                action_type=OperatorActionType.ACTIVATED,
                mismatch_reasons=(),
            ),
            "Kill switch activation conflicted",
        )

    def record_reconciliation(
        self,
        command: ReconcilePaperCommand,
        report: LedgerReconciliationReport,
        *,
        actor: str,
    ) -> Result[PaperReconciliationResult]:
        def operation() -> PaperReconciliationResult:
            if not report.matched or report.account_id != command.account_id:
                raise _Rejected(ErrorCode.CONFLICT, "Reconciliation report is invalid")
            self._require_new_action(command.action_id)
            self._lock_account_ids((command.account_id,))
            global_row = self._switch_row(KillSwitchScope.GLOBAL, None, lock=True)
            action = self._append_action(
                action_id=command.action_id,
                action_type=OperatorActionType.RECONCILED,
                scope=KillSwitchScope.ACCOUNT,
                account_id=command.account_id,
                actor=actor,
                reason_code="reconciliation_passed",
                switch_version=global_row.version,
                cancelled_order_ids=(),
                reconciliation_hashes=(reconciliation_report_hash(report),),
                mismatch_reasons=(),
            )
            return PaperReconciliationResult(
                report=report,
                state=_state_from_row(global_row),
                action=action,
            )

        return self._mutation(operation, "Reconciliation audit conflicted")

    def fail_reconciliation(
        self,
        command: ReconcilePaperCommand,
        *,
        actor: str,
        mismatch_reasons: tuple[str, ...],
    ) -> Result[PaperOperationRecord]:
        return self._mutation(
            lambda: self._activate(
                action_id=command.action_id,
                scope=KillSwitchScope.GLOBAL,
                account_id=None,
                expected_version=None,
                actor=actor,
                reason_code="ledger_reconciliation_failed",
                action_type=OperatorActionType.RECONCILIATION_FAILED,
                mismatch_reasons=tuple(sorted(set(mismatch_reasons))),
            ),
            "Failed reconciliation safety action conflicted",
        )

    def prepare_resume(self, command: ResumePaperCommand) -> Result[ResumePreparation]:
        try:
            self._lock_global_switch()
            row = self._switch_row(command.scope, command.account_id, lock=True)
            if row.version != command.expected_version or not row.active:
                raise _Rejected(ErrorCode.CONFLICT, "Kill switch resume CAS failed")
            account_ids = self._resume_accounts(command.scope, command.account_id)
            return Success(
                ResumePreparation(
                    state=_state_from_row(row),
                    account_ids=account_ids,
                )
            )
        except (ValueError, _Rejected) as error:
            if isinstance(error, _Rejected):
                return _failure(error.code, error.message)
            return _failure(ErrorCode.CONFLICT, "Resume preparation is invalid")

    def complete_resume(
        self,
        command: ResumePaperCommand,
        preparation: ResumePreparation,
        reports: tuple[LedgerReconciliationReport, ...],
        *,
        actor: str,
    ) -> Result[PaperOperationRecord]:
        def operation() -> PaperOperationRecord:
            self._require_new_action(command.action_id)
            row = self._prepared_switch(command, preparation)
            report_accounts = tuple(item.account_id for item in reports)
            if (
                report_accounts != preparation.account_ids
                or not reports
                or any(not item.matched for item in reports)
            ):
                raise _Rejected(ErrorCode.CONFLICT, "Resume reconciliation changed")
            row.active = False
            row.reason_code = command.reason_code
            row.actor = actor
            row.version += 1
            self._session.flush()
            self._session.refresh(row)
            state = _state_from_row(row)
            action = self._append_action(
                action_id=command.action_id,
                action_type=OperatorActionType.RESUMED,
                scope=command.scope,
                account_id=command.account_id,
                actor=actor,
                reason_code=command.reason_code,
                switch_version=state.version,
                cancelled_order_ids=(),
                reconciliation_hashes=tuple(
                    sorted(reconciliation_report_hash(item) for item in reports)
                ),
                mismatch_reasons=(),
            )
            return PaperOperationRecord(state=state, action=action)

        return self._mutation(operation, "Paper resume conflicted")

    def reject_resume(
        self,
        command: ResumePaperCommand,
        preparation: ResumePreparation,
        *,
        actor: str,
        mismatch_reasons: tuple[str, ...],
    ) -> Result[PaperOperatorAction]:
        def operation() -> PaperOperatorAction:
            self._require_new_action(command.action_id)
            row = self._prepared_switch(command, preparation)
            return self._append_action(
                action_id=command.action_id,
                action_type=OperatorActionType.RESUME_REJECTED,
                scope=command.scope,
                account_id=command.account_id,
                actor=actor,
                reason_code="resume_reconciliation_failed",
                switch_version=row.version,
                cancelled_order_ids=(),
                reconciliation_hashes=(),
                mismatch_reasons=tuple(sorted(set(mismatch_reasons))),
            )

        return self._mutation(operation, "Rejected resume audit conflicted")

    def _activate(
        self,
        *,
        action_id: UUID,
        scope: KillSwitchScope,
        account_id: str | None,
        expected_version: int | None,
        actor: str,
        reason_code: str,
        action_type: OperatorActionType,
        mismatch_reasons: tuple[str, ...],
    ) -> PaperOperationRecord:
        self._require_new_action(action_id)
        global_row = self._lock_global_switch()
        row = (
            global_row
            if scope is KillSwitchScope.GLOBAL
            else self._account_switch(account_id, expected_version)
        )
        created = row in self._session.new
        if (
            expected_version is not None
            and not created
            and row.version != expected_version
        ):
            raise _Rejected(ErrorCode.CONFLICT, "Kill switch activation CAS failed")
        row.active = True
        row.reason_code = reason_code
        row.actor = actor
        if not created:
            row.version += 1
        account_ids = self._activation_accounts(scope, account_id)
        cancelled = self._cancel_pending_orders(account_ids, at=self._database_now())
        self._session.flush()
        self._session.refresh(row)
        state = _state_from_row(row)
        action = self._append_action(
            action_id=action_id,
            action_type=action_type,
            scope=scope,
            account_id=account_id,
            actor=actor,
            reason_code=reason_code,
            switch_version=state.version,
            cancelled_order_ids=cancelled,
            reconciliation_hashes=(),
            mismatch_reasons=mismatch_reasons,
        )
        return PaperOperationRecord(state=state, action=action)

    def _account_switch(
        self, account_id: str | None, expected_version: int | None
    ) -> PaperKillSwitchRow:
        if account_id is None:
            raise _Rejected(ErrorCode.INVALID_INPUT, "Account kill switch is invalid")
        self._lock_account_ids((account_id,))
        row = self._session.scalar(
            select(PaperKillSwitchRow)
            .where(
                PaperKillSwitchRow.scope == KillSwitchScope.ACCOUNT.value,
                PaperKillSwitchRow.account_id == account_id,
            )
            .with_for_update()
        )
        if row is not None:
            return row
        if expected_version != 0:
            raise _Rejected(ErrorCode.CONFLICT, "Account kill switch CAS failed")
        now = self._database_now()
        row = PaperKillSwitchRow(
            switch_id=uuid5(NAMESPACE_URL, f"paper-kill-switch:{account_id}"),
            scope=KillSwitchScope.ACCOUNT.value,
            account_id=account_id,
            active=True,
            reason_code="initialized",
            actor="system:operator",
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        return row

    def _cancel_pending_orders(
        self, account_ids: tuple[str, ...], *, at: datetime
    ) -> tuple[UUID, ...]:
        if not account_ids:
            return ()
        self._lock_account_ids(account_ids)
        rows = self._session.scalars(
            select(OrderIntentRow)
            .where(OrderIntentRow.account_id.in_(account_ids))
            .order_by(OrderIntentRow.intent_id)
            .with_for_update()
        ).all()
        cancelled: list[UUID] = []
        for row in rows:
            intent = _intent_from_row(row)
            events = self._order_events(intent.intent_id)
            previous = events[-1] if events else None
            status = previous.to_status if previous is not None else OrderStatus.CREATED
            if status in _TERMINAL_ORDER_STATUSES:
                continue
            target = (
                OrderStatus.EXPIRED
                if at >= intent.valid_until
                else OrderStatus.CANCELLED
            )
            cumulative = (
                previous.cumulative_filled_quantity
                if previous is not None
                else Decimal(0)
            )
            created = append_order_event(
                intent,
                previous=previous,
                target_status=target,
                cumulative_filled_quantity=cumulative,
                occurred_at=at,
                reason="paper_kill_switch_activated",
            )
            if isinstance(created, Failure):
                raise _Rejected(created.error.code, created.error.message)
            self._release_reservation(intent, at=at)
            self._session.add(order_event_row(created.value))
            cancelled.append(intent.intent_id)
        self._session.flush()
        return tuple(sorted(cancelled, key=str))

    def _release_reservation(self, intent: OrderIntent, *, at: datetime) -> None:
        row = self._session.scalar(
            select(AccountReservationRow)
            .where(AccountReservationRow.reservation_id == intent.reservation_id)
            .with_for_update()
        )
        if row is None:
            raise _Rejected(ErrorCode.CONFLICT, "Pending order reservation is missing")
        try:
            before = reservation_from_row(row)
        except ValueError as error:
            raise _Rejected(
                ErrorCode.CONFLICT, "Pending reservation integrity failed"
            ) from error
        if before.state not in {
            ReservationState.OPEN,
            ReservationState.PARTIALLY_CONSUMED,
        }:
            raise _Rejected(ErrorCode.CONFLICT, "Pending reservation is terminal")
        changed = (
            expire_reservation(before, at=at)
            if at >= before.expires_at
            else release_reservation(
                before, at=at, reason="paper_kill_switch_activated"
            )
        )
        if isinstance(changed, Failure):
            raise _Rejected(changed.error.code, changed.error.message)
        after = changed.value.reservation
        self._session.add(reservation_event_row(changed.value.event))
        row.remaining_amount = after.remaining_amount
        row.state = after.state.value
        row.updated_at = after.updated_at
        row.event_sequence = after.event_sequence
        row.previous_event_hash = after.previous_event_hash
        row.event_hash = after.event_hash
        self._release_projection(before, before.remaining_amount)

    def _release_projection(
        self, reservation: AccountReservation, amount: Decimal
    ) -> None:
        updated: object | None
        if reservation.kind is ReservationKind.CASH:
            updated = self._session.scalar(
                update(PaperCashProjectionRow)
                .where(
                    PaperCashProjectionRow.account_id == reservation.account_id,
                    PaperCashProjectionRow.currency == reservation.commodity,
                    PaperCashProjectionRow.updated_sequence
                    == reservation.account_aggregate_sequence,
                    PaperCashProjectionRow.reserved_amount >= amount,
                )
                .values(reserved_amount=PaperCashProjectionRow.reserved_amount - amount)
                .returning(PaperCashProjectionRow)
            )
        else:
            updated = self._session.scalar(
                update(PaperPositionProjectionRow)
                .where(
                    PaperPositionProjectionRow.account_id == reservation.account_id,
                    PaperPositionProjectionRow.instrument_id
                    == reservation.instrument_id,
                    PaperPositionProjectionRow.updated_sequence
                    == reservation.account_aggregate_sequence,
                    PaperPositionProjectionRow.reserved_quantity >= amount,
                )
                .values(
                    reserved_quantity=PaperPositionProjectionRow.reserved_quantity
                    - amount
                )
                .returning(PaperPositionProjectionRow)
            )
        if updated is None:
            raise _Rejected(ErrorCode.CONFLICT, "Reserved projection release failed")

    def _order_events(self, intent_id: UUID) -> tuple[OrderEvent, ...]:
        rows = self._session.scalars(
            select(OrderEventRow)
            .where(OrderEventRow.order_intent_id == intent_id)
            .order_by(OrderEventRow.sequence)
            .with_for_update()
        ).all()
        try:
            events = tuple(order_event_from_row(item) for item in rows)
        except ValueError as error:
            raise _Rejected(
                ErrorCode.CONFLICT, "Order event integrity failed"
            ) from error
        previous_hash: str | None = None
        for sequence, event in enumerate(events, start=1):
            if event.sequence != sequence or event.previous_event_hash != previous_hash:
                raise _Rejected(ErrorCode.CONFLICT, "Order event chain is invalid")
            previous_hash = event.event_hash
        return events

    def _prepared_switch(
        self, command: ResumePaperCommand, preparation: ResumePreparation
    ) -> PaperKillSwitchRow:
        row = self._switch_row(command.scope, command.account_id, lock=True)
        if _state_from_row(row) != preparation.state or not row.active:
            raise _Rejected(ErrorCode.CONFLICT, "Prepared kill switch changed")
        return row

    def _resume_accounts(
        self, scope: KillSwitchScope, account_id: str | None
    ) -> tuple[str, ...]:
        if scope is KillSwitchScope.ACCOUNT:
            assert account_id is not None
            self._lock_account_ids((account_id,))
            return (account_id,)
        rows = self._session.scalars(
            select(PaperAccountRow)
            .order_by(PaperAccountRow.account_id)
            .with_for_update()
        ).all()
        identities = tuple(item.account_id for item in rows)
        if not identities:
            raise _Rejected(
                ErrorCode.DATA_UNAVAILABLE, "No paper accounts to reconcile"
            )
        return identities

    def _activation_accounts(
        self, scope: KillSwitchScope, account_id: str | None
    ) -> tuple[str, ...]:
        if scope is KillSwitchScope.ACCOUNT:
            assert account_id is not None
            return (account_id,)
        return tuple(
            self._session.scalars(
                select(PaperAccountRow.account_id).order_by(PaperAccountRow.account_id)
            ).all()
        )

    def _lock_account_ids(self, account_ids: tuple[str, ...]) -> None:
        if not account_ids:
            return
        rows = self._session.scalars(
            select(PaperAccountRow)
            .where(PaperAccountRow.account_id.in_(account_ids))
            .order_by(PaperAccountRow.account_id)
            .with_for_update()
        ).all()
        if tuple(item.account_id for item in rows) != tuple(sorted(account_ids)):
            raise _Rejected(ErrorCode.NOT_FOUND, "Paper account was not found")

    def _lock_global_switch(self) -> PaperKillSwitchRow:
        return self._switch_row(KillSwitchScope.GLOBAL, None, lock=True)

    def _switch_row(
        self,
        scope: KillSwitchScope,
        account_id: str | None,
        *,
        lock: bool,
    ) -> PaperKillSwitchRow:
        if (scope is KillSwitchScope.GLOBAL) != (account_id is None):
            raise _Rejected(ErrorCode.INVALID_INPUT, "Kill switch scope is invalid")
        query = select(PaperKillSwitchRow).where(
            PaperKillSwitchRow.scope == scope.value,
            PaperKillSwitchRow.account_id.is_(None)
            if account_id is None
            else PaperKillSwitchRow.account_id == account_id,
        )
        if lock:
            query = query.with_for_update()
        rows = self._session.scalars(query).all()
        if len(rows) != 1:
            code = ErrorCode.NOT_FOUND if not rows else ErrorCode.CONFLICT
            raise _Rejected(code, "Paper kill switch was not found")
        return rows[0]

    def _append_action(
        self,
        *,
        action_id: UUID,
        action_type: OperatorActionType,
        scope: KillSwitchScope,
        account_id: str | None,
        actor: str,
        reason_code: str,
        switch_version: int,
        cancelled_order_ids: tuple[UUID, ...],
        reconciliation_hashes: tuple[str, ...],
        mismatch_reasons: tuple[str, ...],
    ) -> PaperOperatorAction:
        head = self._session.scalar(
            select(PaperOperatorAuditHeadRow)
            .where(PaperOperatorAuditHeadRow.head_id == 1)
            .with_for_update()
        )
        if head is None:
            raise _Rejected(ErrorCode.CONFLICT, "Operator audit head is missing")
        action = PaperOperatorAction.create(
            action_id=action_id,
            sequence=head.sequence + 1,
            action_type=action_type,
            scope=scope,
            account_id=account_id,
            actor=actor,
            reason_code=reason_code,
            switch_version=switch_version,
            cancelled_order_ids=tuple(sorted(cancelled_order_ids, key=str)),
            reconciliation_hashes=tuple(sorted(reconciliation_hashes)),
            mismatch_reasons=tuple(sorted(set(mismatch_reasons))),
            occurred_at=self._database_now(),
            previous_action_hash=head.action_hash,
        )
        self._session.add(_action_row(action))
        self._session.flush()
        head.sequence = action.sequence
        head.action_hash = action.action_hash
        self._session.flush()
        return action

    def _verified_actions(self) -> tuple[PaperOperatorAction, ...]:
        rows = self._session.scalars(
            select(PaperOperatorActionRow).order_by(PaperOperatorActionRow.sequence)
        ).all()
        actions: list[PaperOperatorAction] = []
        previous_hash: str | None = None
        for sequence, row in enumerate(rows, start=1):
            try:
                action = PaperOperatorAction.model_validate(row.payload)
            except ValueError as error:
                raise _Rejected(
                    ErrorCode.CONFLICT, "Operator action integrity failed"
                ) from error
            if (
                action.action_id != row.action_id
                or action.sequence != row.sequence
                or action.action_hash != row.action_hash
                or action.sequence != sequence
                or action.previous_action_hash != previous_hash
            ):
                raise _Rejected(ErrorCode.CONFLICT, "Operator action chain is invalid")
            actions.append(action)
            previous_hash = action.action_hash
        head = self._session.get(PaperOperatorAuditHeadRow, 1)
        if (
            head is None
            or head.sequence != len(actions)
            or head.action_hash != previous_hash
        ):
            raise _Rejected(ErrorCode.CONFLICT, "Operator audit head diverged")
        return tuple(actions)

    def _require_new_action(self, action_id: UUID) -> None:
        if self._session.get(PaperOperatorActionRow, action_id) is not None:
            raise _Rejected(ErrorCode.CONFLICT, "Operator action already exists")

    def _database_now(self) -> datetime:
        value = self._session.scalar(select(func.clock_timestamp()))
        if not isinstance(value, datetime):
            raise _Rejected(ErrorCode.INTERNAL_ERROR, "Database clock is unavailable")
        return value

    def _mutation[T](
        self, operation: Callable[[], T], conflict_message: str
    ) -> Result[T]:
        try:
            with self._session.begin_nested():
                return Success(operation())
        except _Rejected as error:
            return _failure(error.code, error.message)
        except (IntegrityError, ValueError):
            return _failure(ErrorCode.CONFLICT, conflict_message)
        except DBAPIError as error:
            code = (
                ErrorCode.CONFLICT
                if getattr(error.orig, "sqlstate", None)
                in {"23503", "23505", "23514", "40001", "55000"}
                else ErrorCode.INTERNAL_ERROR
            )
            return _failure(code, conflict_message)


_TERMINAL_ORDER_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
    }
)


def _intent_from_row(row: OrderIntentRow) -> OrderIntent:
    try:
        intent = OrderIntent.model_validate(row.payload)
    except ValueError as error:
        raise _Rejected(ErrorCode.CONFLICT, "Order intent integrity failed") from error
    if intent.intent_id != row.intent_id or intent.intent_hash != row.intent_hash:
        raise _Rejected(ErrorCode.CONFLICT, "Order intent identity changed")
    return intent


def _state_from_row(row: PaperKillSwitchRow) -> PaperKillSwitchState:
    try:
        return PaperKillSwitchState(
            switch_id=row.switch_id,
            scope=KillSwitchScope(row.scope),
            account_id=row.account_id,
            active=row.active,
            reason_code=row.reason_code,
            actor=row.actor,
            version=row.version,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
    except ValueError as error:
        raise _Rejected(ErrorCode.CONFLICT, "Kill switch state is invalid") from error


def _action_row(action: PaperOperatorAction) -> PaperOperatorActionRow:
    return PaperOperatorActionRow(
        action_id=action.action_id,
        sequence=action.sequence,
        action_type=action.action_type.value,
        scope=action.scope.value,
        account_id=action.account_id,
        actor=action.actor,
        reason_code=action.reason_code,
        switch_version=action.switch_version,
        previous_action_hash=action.previous_action_hash,
        action_hash=action.action_hash,
        payload=action.model_dump(mode="json"),
        occurred_at=action.occurred_at,
    )


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
