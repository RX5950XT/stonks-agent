from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from uuid import UUID

import pytest
from support.telemetry import RecordingOperationRecorder

from stonks_agent.application.execution.execute import execute_reference_paper
from stonks_agent.application.ledger.post import (
    LedgerPostingPolicy,
    build_fill_journal,
)
from stonks_agent.application.ledger.replay import replay_journal
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.execution_model import PaperExecutionOutcome
from stonks_agent.domain.fills import Fill
from stonks_agent.domain.journal import JournalTransaction
from stonks_agent.domain.ledger import LedgerHead, LedgerProjection
from stonks_agent.domain.orders import ExecutionCommand, OrderEvent, OrderIntent
from stonks_agent.domain.portfolio import (
    AccountPortfolioSnapshot,
    CashBalance,
    PaperAccountState,
)
from stonks_agent.domain.reservations import AccountReservation
from stonks_agent.domain.telemetry import ComponentName, OperationName
from stonks_agent.domain.trading_persistence import PaperExecutionRecord

from .helpers import ACCOUNT_ID, command, request, reservation
from .test_paper_broker import broker


def ledger_policy() -> LedgerPostingPolicy:
    return LedgerPostingPolicy(
        policy_version="1.0.0",
        cost_basis_method="average",
        monetary_rounding="ROUND_HALF_EVEN",
    )


def _failure(code: ErrorCode = ErrorCode.CONFLICT) -> Failure:
    return Failure(StructuredError(code=code, message="simulated failure"))


def outcome() -> PaperExecutionOutcome:
    result = broker().execute(request())
    assert isinstance(result, Success)
    return result.value


class FakeExecutionRepository:
    def __init__(self) -> None:
        self.intent = command().intent
        self.reservation = reservation()
        self.account = PaperAccountState.model_construct(
            account_id=ACCOUNT_ID,
            base_currency="USD",
            account_aggregate_sequence=8,
            portfolio_sequence=3,
            ledger_sequence=0,
            ledger_hash=None,
            cash=(),
            positions=(),
            events=(),
            created_at=self.intent.created_at,
            updated_at=self.intent.created_at,
        )
        self.events: tuple[OrderEvent, ...] = ()
        self.fills: tuple[Fill, ...] = ()
        self.record: PaperExecutionRecord | None = None
        self.fail_at: str | None = None

    def get_execution_record(
        self, *, account_id: str, idempotency_key: str
    ) -> Result[PaperExecutionRecord]:
        if self.fail_at == "record":
            return _failure()
        if self.record is None:
            return _failure(ErrorCode.NOT_FOUND)
        return Success(self.record)

    def get_order_by_idempotency(
        self, *, account_id: str, idempotency_key: str
    ) -> Result[OrderIntent]:
        return _failure() if self.fail_at == "intent" else Success(self.intent)

    def get_reservation(self, reservation_id: object) -> Result[AccountReservation]:
        return (
            _failure() if self.fail_at == "reservation" else Success(self.reservation)
        )

    def get_account(self, account_id: str) -> Result[PaperAccountState]:
        return _failure() if self.fail_at == "account" else Success(self.account)

    def list_order_events(self, intent_id: object) -> Result[tuple[OrderEvent, ...]]:
        return _failure() if self.fail_at == "events" else Success(self.events)

    def list_fills(self, intent_id: object) -> Result[tuple[Fill, ...]]:
        return _failure() if self.fail_at == "fills" else Success(self.fills)

    def apply_paper_execution(
        self,
        execution_command: ExecutionCommand,
        simulated: PaperExecutionOutcome,
        *,
        expected_account_sequence: int,
    ) -> Result[PaperExecutionRecord]:
        if self.fail_at == "persist":
            return _failure()
        self.record = PaperExecutionRecord(
            account_id=execution_command.intent.account_id,
            idempotency_key=execution_command.intent.idempotency_key,
            command_id=execution_command.command_id,
            command_hash=execution_command.command_hash,
            intent_hash=execution_command.intent.intent_hash,
            outcome=simulated,
        )
        return Success(self.record)


class FakeLedgerRepository:
    def __init__(self) -> None:
        self.opening = AccountPortfolioSnapshot(
            snapshot_id=UUID("45000000-0000-4000-8000-000000000099"),
            account_id=ACCOUNT_ID,
            as_of=datetime(2026, 7, 13, 14, 0, tzinfo=UTC),
            account_aggregate_sequence=0,
            portfolio_sequence=0,
            ledger_sequence=0,
            ledger_hash=None,
            cash=(
                CashBalance(
                    currency="USD",
                    settled_amount=Decimal("10000.00"),
                    reserved_amount=Decimal("0.00"),
                    quantum=Decimal("0.01"),
                ),
            ),
        )
        self.transactions: list[JournalTransaction] = []
        self.active = False

    def get_head(self, account_id: str) -> Result[LedgerHead]:
        projection = self.get_projection(account_id)
        assert isinstance(projection, Success)
        return Success(
            LedgerHead(
                account_id=account_id,
                sequence=projection.value.ledger_sequence,
                transaction_hash=projection.value.ledger_hash,
            )
        )

    def get_opening_snapshot(self, account_id: str) -> Result[AccountPortfolioSnapshot]:
        return Success(self.opening)

    def get_projection(self, account_id: str) -> Result[LedgerProjection]:
        return replay_journal(self.opening, tuple(self.transactions))

    def append(
        self,
        transaction: JournalTransaction,
        *,
        expected_sequence: int,
        expected_hash: str | None,
        expected_account_sequence: int,
    ) -> Result[JournalTransaction]:
        if transaction in self.transactions:
            return Success(transaction)
        self.transactions.append(transaction)
        return Success(transaction)

    def list_transactions(
        self, account_id: str, *, after_sequence: int = 0
    ) -> Result[tuple[JournalTransaction, ...]]:
        return Success(tuple(self.transactions[after_sequence:]))

    def validate_execution_graph(self, record: PaperExecutionRecord) -> Result[bool]:
        sources = {item.source_fill_id for item in self.transactions}
        if all(fill.fill_id in sources for fill in record.outcome.receipt.fills):
            return Success(True)
        return _failure()

    def validate_account_graph(self, account_id: str) -> Result[bool]:
        return Success(True)

    def execution_enabled(self, account_id: str) -> Result[bool]:
        return _failure() if self.active else Success(True)

    def activate_global_kill_switch(
        self, *, reason_code: str, actor: str
    ) -> Result[bool]:
        self.active = True
        return Success(True)


class FakeUnitOfWork:
    def __init__(self, repository: FakeExecutionRepository) -> None:
        self.trading = repository
        self.ledger = FakeLedgerRepository()
        self.committed = False

    def __enter__(self) -> FakeUnitOfWork:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.committed = False


class FailingModel:
    def execute(self, candidate: object) -> Result[PaperExecutionOutcome]:
        return _failure(ErrorCode.DATA_UNAVAILABLE)


def test_use_case_rehydrates_authority_and_commits_once() -> None:
    repository = FakeExecutionRepository()
    unit_of_work = FakeUnitOfWork(repository)
    telemetry = RecordingOperationRecorder()

    result = execute_reference_paper(
        request(),
        broker(),
        ledger_policy(),
        lambda: unit_of_work,
        telemetry=telemetry,
    )

    assert isinstance(result, Success)
    assert unit_of_work.committed is True
    assert result.value == repository.record
    assert telemetry.calls == [(ComponentName.EXECUTION, OperationName.EXECUTE)]


def test_existing_exact_receipt_replays_without_second_commit() -> None:
    repository = FakeExecutionRepository()
    exact_outcome = outcome()
    execution_command = command()
    repository.record = PaperExecutionRecord(
        account_id=ACCOUNT_ID,
        idempotency_key=execution_command.intent.idempotency_key,
        command_id=execution_command.command_id,
        command_hash=execution_command.command_hash,
        intent_hash=execution_command.intent.intent_hash,
        outcome=exact_outcome,
    )
    unit_of_work = FakeUnitOfWork(repository)
    projection = unit_of_work.ledger.get_projection(ACCOUNT_ID)
    assert isinstance(projection, Success)
    journal = build_fill_journal(
        exact_outcome.receipt.fills[0], projection.value, ledger_policy()
    )
    assert isinstance(journal, Success)
    unit_of_work.ledger.transactions.append(journal.value)

    result = execute_reference_paper(
        request(), broker(), ledger_policy(), lambda: unit_of_work
    )

    assert isinstance(result, Success)
    assert result.value == repository.record
    assert unit_of_work.committed is False


def test_existing_receipt_without_journal_activates_global_kill_switch() -> None:
    repository = FakeExecutionRepository()
    execution_command = command()
    repository.record = PaperExecutionRecord(
        account_id=ACCOUNT_ID,
        idempotency_key=execution_command.intent.idempotency_key,
        command_id=execution_command.command_id,
        command_hash=execution_command.command_hash,
        intent_hash=execution_command.intent.intent_hash,
        outcome=outcome(),
    )
    unit_of_work = FakeUnitOfWork(repository)

    result = execute_reference_paper(
        request(), broker(), ledger_policy(), lambda: unit_of_work
    )

    assert isinstance(result, Failure)
    assert unit_of_work.ledger.active
    assert unit_of_work.committed


def test_idempotency_or_authority_drift_fails_without_commit() -> None:
    repository = FakeExecutionRepository()
    execution_command = command()
    repository.record = PaperExecutionRecord(
        account_id=ACCOUNT_ID,
        idempotency_key=execution_command.intent.idempotency_key,
        command_id=execution_command.command_id,
        command_hash="f" * 64,
        intent_hash=execution_command.intent.intent_hash,
        outcome=outcome(),
    )
    idempotent_uow = FakeUnitOfWork(repository)
    idempotent = execute_reference_paper(
        request(), broker(), ledger_policy(), lambda: idempotent_uow
    )
    repository.record = None
    repository.intent = repository.intent.model_copy(update={"intent_hash": "f" * 64})
    authority_uow = FakeUnitOfWork(repository)
    authority = execute_reference_paper(
        request(), broker(), ledger_policy(), lambda: authority_uow
    )

    assert isinstance(idempotent, Failure)
    assert isinstance(authority, Failure)
    assert idempotent.error.code is ErrorCode.CONFLICT
    assert authority.error.code is ErrorCode.CONFLICT
    assert idempotent_uow.committed is False
    assert authority_uow.committed is False


@pytest.mark.parametrize(
    "fail_at",
    ("record", "intent", "reservation", "account", "events", "fills", "persist"),
)
def test_dependency_failure_propagates_without_commit(fail_at: str) -> None:
    repository = FakeExecutionRepository()
    repository.fail_at = fail_at
    unit_of_work = FakeUnitOfWork(repository)

    result = execute_reference_paper(
        request(), broker(), ledger_policy(), lambda: unit_of_work
    )

    assert isinstance(result, Failure)
    assert unit_of_work.committed is False


def test_sequence_rehydration_and_model_failure_are_fail_closed() -> None:
    sequence_repository = FakeExecutionRepository()
    sequence_repository.account = sequence_repository.account.model_copy(
        update={"account_aggregate_sequence": 9}
    )
    sequence_uow = FakeUnitOfWork(sequence_repository)
    stale = execute_reference_paper(
        request(), broker(), ledger_policy(), lambda: sequence_uow
    )
    model_repository = FakeExecutionRepository()
    model_uow = FakeUnitOfWork(model_repository)
    unavailable = execute_reference_paper(
        request(), FailingModel(), ledger_policy(), lambda: model_uow
    )

    assert isinstance(stale, Failure)
    assert stale.error.code is ErrorCode.CONFLICT
    assert isinstance(unavailable, Failure)
    assert unavailable.error.code is ErrorCode.DATA_UNAVAILABLE
    assert sequence_uow.committed is False
    assert model_uow.committed is False
