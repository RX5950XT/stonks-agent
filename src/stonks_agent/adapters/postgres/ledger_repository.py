"""PostgreSQL persistence for atomic balanced journal projections."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from stonks_agent.adapters.postgres.models import (
    JournalPostingRow,
    JournalTransactionRow,
    OrderEventRow,
    PaperAccountEventRow,
    PaperAccountOpeningSnapshotRow,
    PaperAccountRow,
    PaperCashProjectionRow,
    PaperFillRow,
    PaperKillSwitchRow,
    PaperLedgerAccountProjectionRow,
    PaperPositionProjectionRow,
)
from stonks_agent.adapters.postgres.trading_mapping import (
    account_event_row,
    journal_row,
    new_account_event,
    posting_from_row,
    posting_row,
)
from stonks_agent.application.ledger.post import LedgerPostingPolicy, build_fill_journal
from stonks_agent.application.ledger.replay import replay_journal
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.fills import Fill
from stonks_agent.domain.journal import JournalTransaction
from stonks_agent.domain.ledger import (
    LedgerAccountBalance,
    LedgerHead,
    LedgerProjection,
)
from stonks_agent.domain.portfolio import AccountPortfolioSnapshot
from stonks_agent.domain.trading_persistence import PaperExecutionRecord

ZERO = Decimal(0)


class _Rejected(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def register_opening_projection(
    session: Session, snapshot: AccountPortfolioSnapshot
) -> None:
    session.add(
        PaperAccountOpeningSnapshotRow(
            account_id=snapshot.account_id,
            snapshot_id=snapshot.snapshot_id,
            snapshot_hash=snapshot.snapshot_hash,
            payload=snapshot.model_dump(mode="json"),
            created_at=snapshot.as_of,
        )
    )
    for cash in snapshot.cash:
        session.add(
            _projection_row(
                account_id=snapshot.account_id,
                ledger_account=f"asset:cash:{cash.currency}",
                commodity=cash.currency,
                quantum=cash.quantum,
                debit_total=cash.settled_amount,
                credit_total=ZERO,
                sequence=0,
                updated_at=snapshot.as_of,
            )
        )
    for position in snapshot.positions:
        commodity = str(position.instrument_id)
        session.add(
            _projection_row(
                account_id=snapshot.account_id,
                ledger_account=f"inventory:units:{commodity}",
                commodity=commodity,
                quantum=position.quantum,
                debit_total=position.quantity,
                credit_total=ZERO,
                sequence=0,
                updated_at=snapshot.as_of,
            )
        )


class PostgresLedgerRepository:
    """Flush-only ledger repository; the core unit of work owns commit."""

    def __init__(
        self, session: Session, policy: LedgerPostingPolicy | None = None
    ) -> None:
        self._session = session
        self._policy = policy or LedgerPostingPolicy(
            policy_version="1.0.0",
            cost_basis_method="average",
            monetary_rounding="ROUND_HALF_EVEN",
        )

    def get_head(self, account_id: str) -> Result[LedgerHead]:
        row = self._session.get(PaperAccountRow, account_id)
        if row is None:
            return _failure(ErrorCode.NOT_FOUND, "Paper account was not found")
        try:
            return Success(
                LedgerHead(
                    account_id=account_id,
                    sequence=row.ledger_sequence,
                    transaction_hash=row.ledger_hash,
                )
            )
        except ValueError:
            return _failure(ErrorCode.CONFLICT, "Ledger head is invalid")

    def get_opening_snapshot(self, account_id: str) -> Result[AccountPortfolioSnapshot]:
        row = self._session.get(PaperAccountOpeningSnapshotRow, account_id)
        if row is None:
            return _failure(ErrorCode.NOT_FOUND, "Ledger opening was not found")
        try:
            snapshot = AccountPortfolioSnapshot.model_validate(row.payload)
        except ValueError:
            return _failure(ErrorCode.CONFLICT, "Ledger opening is invalid")
        if (
            snapshot.account_id != row.account_id
            or snapshot.snapshot_id != row.snapshot_id
            or snapshot.snapshot_hash != row.snapshot_hash
        ):
            return _failure(ErrorCode.CONFLICT, "Ledger opening identity changed")
        return Success(snapshot)

    def get_projection(self, account_id: str) -> Result[LedgerProjection]:
        opening = self.get_opening_snapshot(account_id)
        head = self.get_head(account_id)
        if isinstance(opening, Failure):
            return opening
        if isinstance(head, Failure):
            return head
        transactions = self.list_transactions(account_id)
        if isinstance(transactions, Failure):
            return transactions
        rows = self._session.scalars(
            select(PaperLedgerAccountProjectionRow)
            .where(PaperLedgerAccountProjectionRow.account_id == account_id)
            .order_by(
                PaperLedgerAccountProjectionRow.ledger_account,
                PaperLedgerAccountProjectionRow.commodity,
            )
        ).all()
        replayed = replay_journal(opening.value, transactions.value)
        if isinstance(replayed, Failure):
            return replayed
        try:
            persisted = LedgerProjection.create(
                account_id=account_id,
                opening_snapshot_hash=opening.value.snapshot_hash,
                ledger_sequence=head.value.sequence,
                ledger_hash=head.value.transaction_hash,
                last_occurred_at=replayed.value.last_occurred_at,
                balances=tuple(_balance_from_row(item) for item in rows),
                unvalued_instrument_ids=replayed.value.unvalued_instrument_ids,
            )
        except ValueError:
            return _failure(ErrorCode.CONFLICT, "Ledger projection is invalid")
        if (
            replayed.value.ledger_sequence != head.value.sequence
            or replayed.value.ledger_hash != head.value.transaction_hash
        ):
            return _failure(ErrorCode.CONFLICT, "Ledger head and journal diverged")
        specialized = self._validate_specialized_projection(persisted)
        if isinstance(specialized, Failure):
            return specialized
        return Success(persisted)

    def append(
        self,
        transaction: JournalTransaction,
        *,
        expected_sequence: int,
        expected_hash: str | None,
        expected_account_sequence: int,
    ) -> Result[JournalTransaction]:
        existing = self._existing_transaction(transaction.transaction_id)
        if existing is not None:
            return (
                Success(existing)
                if existing == transaction
                else _failure(ErrorCode.CONFLICT, "Journal identity already exists")
            )

        def operation() -> JournalTransaction:
            account = self._locked_account(transaction.account_id)
            if (
                account.aggregate_sequence != expected_account_sequence
                or account.ledger_sequence != expected_sequence
                or account.ledger_hash != expected_hash
                or transaction.sequence != expected_sequence + 1
                or transaction.previous_hash != expected_hash
            ):
                raise _Rejected(ErrorCode.CONFLICT, "Ledger head CAS failed")
            self._require_execution_enabled(transaction.account_id)
            opening, transactions, before = self._replay_inputs(transaction.account_id)
            fill = self._validate_source(transaction)
            expected = build_fill_journal(fill, before, self._policy)
            if isinstance(expected, Failure) or expected.value != transaction:
                raise _Rejected(ErrorCode.CONFLICT, "Journal economics mismatch fill")
            persisted = self.get_projection(transaction.account_id)
            if isinstance(persisted, Failure):
                raise _Rejected(persisted.error.code, persisted.error.message)
            if persisted.value.projection_hash != before.projection_hash:
                raise _Rejected(ErrorCode.CONFLICT, "Ledger projection drifted")
            after_result = replay_journal(opening, (*transactions, transaction))
            if isinstance(after_result, Failure):
                raise _Rejected(after_result.error.code, after_result.error.message)
            after = after_result.value
            self._session.add(journal_row(transaction))
            self._session.flush()
            self._session.add_all(
                posting_row(transaction.transaction_id, index, posting)
                for index, posting in enumerate(transaction.postings)
            )
            updated = self._advance_account(account, transaction)
            self._apply_settled_projections(before, after, updated.aggregate_sequence)
            self._apply_ledger_projection(after)
            event = new_account_event(
                account_id=transaction.account_id,
                sequence=updated.aggregate_sequence,
                event_type="journal.posted",
                aggregate_ref_type="journal_transaction",
                aggregate_ref_id=transaction.transaction_id,
                occurred_at=updated.updated_at,
                previous_hash=self._previous_account_hash(transaction.account_id),
            )
            self._session.add(account_event_row(event))
            self._session.flush()
            return transaction

        return self._mutation(operation, "Ledger append conflicted")

    def list_transactions(
        self, account_id: str, *, after_sequence: int = 0
    ) -> Result[tuple[JournalTransaction, ...]]:
        if after_sequence < 0:
            return _failure(ErrorCode.INVALID_INPUT, "Ledger sequence is invalid")
        rows = self._session.scalars(
            select(JournalTransactionRow)
            .where(
                JournalTransactionRow.account_id == account_id,
                JournalTransactionRow.sequence > after_sequence,
            )
            .order_by(JournalTransactionRow.sequence)
        ).all()
        parsed: list[JournalTransaction] = []
        for row in rows:
            transaction = self._transaction_from_row(row)
            if transaction is None:
                return _failure(ErrorCode.CONFLICT, "Journal integrity check failed")
            parsed.append(transaction)
        return Success(tuple(parsed))

    def validate_execution_graph(self, record: PaperExecutionRecord) -> Result[bool]:
        if not record.outcome.receipt.fills:
            return Success(True)
        for fill in record.outcome.receipt.fills:
            rows = self._session.scalars(
                select(JournalTransactionRow).where(
                    JournalTransactionRow.source_fill_id == fill.fill_id
                )
            ).all()
            if len(rows) != 1 or rows[0].source_order_intent_id != fill.order_intent_id:
                return _failure(
                    ErrorCode.CONFLICT, "Execution journal graph is incomplete"
                )
        return self.validate_account_graph(record.account_id)

    def validate_account_graph(self, account_id: str) -> Result[bool]:
        fills = self._session.scalars(
            select(PaperFillRow).where(PaperFillRow.account_id == account_id)
        ).all()
        for fill in fills:
            count = len(
                self._session.scalars(
                    select(JournalTransactionRow).where(
                        JournalTransactionRow.source_fill_id == fill.fill_id
                    )
                ).all()
            )
            if count != 1:
                return _failure(
                    ErrorCode.CONFLICT, "Account fill/journal graph is incomplete"
                )
        projection = self.get_projection(account_id)
        if isinstance(projection, Failure):
            return projection
        try:
            _, _, replayed = self._replay_inputs(account_id)
        except _Rejected as error:
            return _failure(error.code, error.message)
        if projection.value.projection_hash != replayed.projection_hash:
            return _failure(ErrorCode.CONFLICT, "Ledger projection replay mismatched")
        try:
            self._validate_all_fill_states(account_id)
        except _Rejected as error:
            return _failure(error.code, error.message)
        return Success(True)

    def execution_enabled(self, account_id: str) -> Result[bool]:
        try:
            self._require_execution_enabled(account_id)
        except _Rejected as error:
            return _failure(error.code, error.message)
        return Success(True)

    def activate_global_kill_switch(
        self, *, reason_code: str, actor: str
    ) -> Result[bool]:
        if not reason_code or not actor:
            return _failure(ErrorCode.INVALID_INPUT, "Kill switch audit is required")

        def operation() -> bool:
            rows = self._session.scalars(
                select(PaperKillSwitchRow)
                .where(PaperKillSwitchRow.scope == "global")
                .with_for_update()
            ).all()
            if len(rows) != 1:
                raise _Rejected(ErrorCode.CONFLICT, "Global kill switch is invalid")
            row = rows[0]
            if row.active and row.reason_code == reason_code:
                return True
            row.active = True
            row.reason_code = reason_code
            row.actor = actor
            row.version += 1
            self._session.flush()
            return True

        return self._mutation(operation, "Global kill switch activation failed")

    def _replay_inputs(
        self, account_id: str
    ) -> tuple[
        AccountPortfolioSnapshot,
        tuple[JournalTransaction, ...],
        LedgerProjection,
    ]:
        opening = self.get_opening_snapshot(account_id)
        transactions = self.list_transactions(account_id)
        if isinstance(opening, Failure):
            raise _Rejected(opening.error.code, opening.error.message)
        if isinstance(transactions, Failure):
            raise _Rejected(transactions.error.code, transactions.error.message)
        replayed = replay_journal(opening.value, transactions.value)
        if isinstance(replayed, Failure):
            raise _Rejected(replayed.error.code, replayed.error.message)
        return opening.value, transactions.value, replayed.value

    def _locked_account(self, account_id: str) -> PaperAccountRow:
        row = self._session.scalar(
            select(PaperAccountRow)
            .where(PaperAccountRow.account_id == account_id)
            .with_for_update()
        )
        if row is None:
            raise _Rejected(ErrorCode.NOT_FOUND, "Paper account was not found")
        return row

    def _advance_account(
        self, account: PaperAccountRow, transaction: JournalTransaction
    ) -> PaperAccountRow:
        row = self._session.scalar(
            update(PaperAccountRow)
            .where(
                PaperAccountRow.account_id == account.account_id,
                PaperAccountRow.aggregate_sequence == account.aggregate_sequence,
                PaperAccountRow.ledger_sequence == account.ledger_sequence,
                PaperAccountRow.ledger_hash.is_not_distinct_from(account.ledger_hash),
            )
            .values(
                aggregate_sequence=account.aggregate_sequence + 1,
                ledger_sequence=transaction.sequence,
                ledger_hash=transaction.transaction_hash,
            )
            .returning(PaperAccountRow)
        )
        if row is None:
            raise _Rejected(ErrorCode.CONFLICT, "Ledger account CAS failed")
        self._session.refresh(row)
        return row

    def _apply_settled_projections(
        self,
        before: LedgerProjection,
        after: LedgerProjection,
        account_sequence: int,
    ) -> None:
        self._apply_cash(before, after, account_sequence)
        self._apply_positions(before, after, account_sequence)
        self._session.flush()

    def _validate_specialized_projection(
        self, projection: LedgerProjection
    ) -> Result[bool]:
        account = self._session.get(PaperAccountRow, projection.account_id)
        if account is None:
            return _failure(ErrorCode.NOT_FOUND, "Paper account was not found")
        cash_rows = self._session.scalars(
            select(PaperCashProjectionRow).where(
                PaperCashProjectionRow.account_id == projection.account_id
            )
        ).all()
        cash = {item.currency: item for item in cash_rows}
        cash_quantums = _cash_quantums(projection)
        if set(cash) != set(cash_quantums) or any(
            item.settled_amount != projection.cash(currency)
            or item.quantum != cash_quantums[currency]
            or item.updated_sequence != account.aggregate_sequence
            for currency, item in cash.items()
        ):
            return _failure(ErrorCode.CONFLICT, "Cash ledger projection mismatched")
        position_rows = self._session.scalars(
            select(PaperPositionProjectionRow).where(
                PaperPositionProjectionRow.account_id == projection.account_id
            )
        ).all()
        positions = {item.instrument_id: item for item in position_rows}
        position_quantums = _position_quantums(projection)
        if set(positions) != set(position_quantums) or any(
            item.quantity != projection.position(instrument_id)
            or item.quantum != position_quantums[instrument_id]
            or item.updated_sequence != account.aggregate_sequence
            for instrument_id, item in positions.items()
        ):
            return _failure(ErrorCode.CONFLICT, "Position ledger projection mismatched")
        return Success(True)

    def _apply_cash(
        self, before: LedgerProjection, after: LedgerProjection, sequence: int
    ) -> None:
        rows = self._session.scalars(
            select(PaperCashProjectionRow)
            .where(PaperCashProjectionRow.account_id == before.account_id)
            .with_for_update()
        ).all()
        existing = {item.currency: item for item in rows}
        quantums = _cash_quantums(after)
        if set(existing) - set(quantums):
            raise _Rejected(ErrorCode.CONFLICT, "Cash projection identity drifted")
        for currency, quantum in quantums.items():
            row = existing.get(currency)
            amount = after.cash(currency)
            if row is None:
                if before.cash(currency) != 0:
                    raise _Rejected(ErrorCode.CONFLICT, "Cash projection is missing")
                self._session.add(
                    PaperCashProjectionRow(
                        account_id=before.account_id,
                        currency=currency,
                        settled_amount=amount,
                        reserved_amount=ZERO,
                        quantum=quantum,
                        updated_sequence=sequence,
                        updated_at=after.last_occurred_at,
                    )
                )
            else:
                if (
                    row.settled_amount != before.cash(currency)
                    or row.quantum != quantum
                ):
                    raise _Rejected(ErrorCode.CONFLICT, "Cash projection drifted")
                row.settled_amount = amount
                row.updated_sequence = sequence

    def _apply_positions(
        self, before: LedgerProjection, after: LedgerProjection, sequence: int
    ) -> None:
        rows = self._session.scalars(
            select(PaperPositionProjectionRow)
            .where(PaperPositionProjectionRow.account_id == before.account_id)
            .with_for_update()
        ).all()
        existing = {item.instrument_id: item for item in rows}
        quantums = _position_quantums(after)
        if set(existing) - set(quantums):
            raise _Rejected(ErrorCode.CONFLICT, "Position projection identity drifted")
        for instrument_id, quantum in quantums.items():
            row = existing.get(instrument_id)
            quantity = after.position(instrument_id)
            if row is None:
                if before.position(instrument_id) != 0:
                    raise _Rejected(
                        ErrorCode.CONFLICT, "Position projection is missing"
                    )
                self._session.add(
                    PaperPositionProjectionRow(
                        account_id=before.account_id,
                        instrument_id=instrument_id,
                        quantity=quantity,
                        sellable_quantity=quantity,
                        reserved_quantity=ZERO,
                        quantum=quantum,
                        updated_sequence=sequence,
                        updated_at=after.last_occurred_at,
                    )
                )
            else:
                if (
                    row.quantity != before.position(instrument_id)
                    or row.quantum != quantum
                ):
                    raise _Rejected(ErrorCode.CONFLICT, "Position projection drifted")
                if row.reserved_quantity > quantity:
                    raise _Rejected(
                        ErrorCode.CONFLICT, "Position reservation exceeds fill"
                    )
                row.quantity = quantity
                row.sellable_quantity = quantity
                row.updated_sequence = sequence

    def _apply_ledger_projection(self, projection: LedgerProjection) -> None:
        rows = self._session.scalars(
            select(PaperLedgerAccountProjectionRow)
            .where(PaperLedgerAccountProjectionRow.account_id == projection.account_id)
            .with_for_update()
        ).all()
        existing = {(item.ledger_account, item.commodity): item for item in rows}
        expected = {
            (item.ledger_account, item.commodity) for item in projection.balances
        }
        if set(existing) - expected:
            raise _Rejected(ErrorCode.CONFLICT, "Ledger account projection drifted")
        for balance in projection.balances:
            key = (balance.ledger_account, balance.commodity)
            row = existing.get(key)
            if row is None:
                self._session.add(
                    _projection_row(
                        account_id=projection.account_id,
                        ledger_account=balance.ledger_account,
                        commodity=balance.commodity,
                        quantum=balance.quantum,
                        debit_total=balance.debit_total,
                        credit_total=balance.credit_total,
                        sequence=projection.ledger_sequence,
                        updated_at=projection.last_occurred_at,
                    )
                )
            elif (
                row.debit_total != balance.debit_total
                or row.credit_total != balance.credit_total
            ):
                if row.quantum != balance.quantum:
                    raise _Rejected(ErrorCode.CONFLICT, "Ledger quantum drifted")
                row.debit_total = balance.debit_total
                row.credit_total = balance.credit_total
                row.updated_ledger_sequence = projection.ledger_sequence
        self._session.flush()

    def _validate_source(self, transaction: JournalTransaction) -> Fill:
        row = self._session.get(PaperFillRow, transaction.source_fill_id)
        if row is None:
            raise _Rejected(ErrorCode.NOT_FOUND, "Journal source was not found")
        if (
            row.order_intent_id != transaction.source_order_intent_id
            or row.account_id != transaction.account_id
        ):
            raise _Rejected(ErrorCode.CONFLICT, "Journal source binding mismatch")
        try:
            fill = Fill.model_validate(row.payload)
        except ValueError as error:
            raise _Rejected(
                ErrorCode.CONFLICT, "Paper fill payload is invalid"
            ) from error
        if not _fill_matches_row(fill, row):
            raise _Rejected(ErrorCode.CONFLICT, "Paper fill indexed values changed")
        self._validate_fill_state(row)
        return fill

    def _validate_fill_state(self, fill: PaperFillRow) -> None:
        latest = self._session.scalar(
            select(OrderEventRow)
            .where(OrderEventRow.order_intent_id == fill.order_intent_id)
            .order_by(OrderEventRow.sequence.desc())
            .limit(1)
        )
        total = sum(
            self._session.scalars(
                select(PaperFillRow.quantity).where(
                    PaperFillRow.order_intent_id == fill.order_intent_id
                )
            ).all(),
            ZERO,
        )
        if (
            latest is None
            or latest.to_status not in {"partially_filled", "filled"}
            or latest.cumulative_filled_quantity != total
        ):
            raise _Rejected(ErrorCode.CONFLICT, "Paper fill order state is unknown")

    def _validate_all_fill_states(self, account_id: str) -> None:
        fills = self._session.scalars(
            select(PaperFillRow).where(PaperFillRow.account_id == account_id)
        ).all()
        for fill in fills:
            self._validate_fill_state(fill)

    def _require_execution_enabled(self, account_id: str) -> None:
        global_rows = self._session.scalars(
            select(PaperKillSwitchRow)
            .where(PaperKillSwitchRow.scope == "global")
            .with_for_update()
        ).all()
        if len(global_rows) != 1 or global_rows[0].active:
            raise _Rejected(ErrorCode.CONFLICT, "Global paper kill switch is active")
        account_rows = self._session.scalars(
            select(PaperKillSwitchRow)
            .where(
                PaperKillSwitchRow.scope == "account",
                PaperKillSwitchRow.account_id == account_id,
            )
            .with_for_update()
        ).all()
        if len(account_rows) > 1 or (account_rows and account_rows[0].active):
            raise _Rejected(ErrorCode.CONFLICT, "Account paper kill switch is active")

    def _previous_account_hash(self, account_id: str) -> str | None:
        return self._session.scalar(
            select(PaperAccountEventRow.event_hash)
            .where(PaperAccountEventRow.account_id == account_id)
            .order_by(PaperAccountEventRow.sequence.desc())
            .limit(1)
        )

    def _existing_transaction(self, transaction_id: UUID) -> JournalTransaction | None:
        row = self._session.get(JournalTransactionRow, transaction_id)
        return None if row is None else self._transaction_from_row(row)

    def _transaction_from_row(
        self, row: JournalTransactionRow
    ) -> JournalTransaction | None:
        postings = self._session.scalars(
            select(JournalPostingRow)
            .where(JournalPostingRow.transaction_id == row.transaction_id)
            .order_by(JournalPostingRow.posting_index)
        ).all()
        try:
            return JournalTransaction(
                transaction_id=row.transaction_id,
                account_id=row.account_id,
                sequence=row.sequence,
                occurred_at=row.occurred_at,
                previous_hash=row.previous_hash,
                source_order_intent_id=row.source_order_intent_id,
                source_fill_id=row.source_fill_id,
                postings=tuple(posting_from_row(item) for item in postings),
                transaction_hash=row.transaction_hash,
            )
        except ValueError:
            return None

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
                in {"23505", "23514", "40001", "55000"}
                else ErrorCode.INTERNAL_ERROR
            )
            return _failure(code, conflict_message)


def _projection_row(
    *,
    account_id: str,
    ledger_account: str,
    commodity: str,
    quantum: Decimal,
    debit_total: Decimal,
    credit_total: Decimal,
    sequence: int,
    updated_at: datetime,
) -> PaperLedgerAccountProjectionRow:
    return PaperLedgerAccountProjectionRow(
        account_id=account_id,
        ledger_account=ledger_account,
        commodity=commodity,
        quantum=quantum,
        debit_total=debit_total,
        credit_total=credit_total,
        updated_ledger_sequence=sequence,
        updated_at=updated_at,
    )


def _balance_from_row(row: PaperLedgerAccountProjectionRow) -> LedgerAccountBalance:
    return LedgerAccountBalance(
        ledger_account=row.ledger_account,
        commodity=row.commodity,
        quantum=row.quantum,
        debit_total=row.debit_total,
        credit_total=row.credit_total,
    )


def _cash_quantums(projection: LedgerProjection) -> dict[str, Decimal]:
    return {
        item.commodity: item.quantum
        for item in projection.balances
        if item.ledger_account == f"asset:cash:{item.commodity}"
    }


def _position_quantums(projection: LedgerProjection) -> dict[UUID, Decimal]:
    return {
        UUID(item.commodity): item.quantum
        for item in projection.balances
        if item.ledger_account == f"inventory:units:{item.commodity}"
    }


def _fill_matches_row(fill: Fill, row: PaperFillRow) -> bool:
    return (
        fill.fill_id == row.fill_id
        and fill.command_id == row.command_id
        and fill.order_intent_id == row.order_intent_id
        and fill.account_id == row.account_id
        and fill.instrument_id == row.instrument_id
        and fill.side.value == row.side
        and fill.quantity == row.quantity
        and fill.quantity_quantum == row.quantity_quantum
        and fill.price == row.price
        and fill.price_quantum == row.price_quantum
        and fill.fee_currency == row.fee_currency
        and fill.fees == row.fees
        and fill.fee_quantum == row.fee_quantum
        and fill.slippage == row.slippage
        and fill.occurred_at == row.occurred_at
    )


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
