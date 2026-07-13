from __future__ import annotations

from types import TracebackType

import pytest

from stonks_agent.application.execution.execute import execute_reference_paper
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
from stonks_agent.domain.portfolio import PaperAccountState
from stonks_agent.domain.reservations import AccountReservation
from stonks_agent.domain.trading_persistence import PaperExecutionRecord

from .helpers import ACCOUNT_ID, command, request, reservation
from .test_paper_broker import broker


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


class FakeUnitOfWork:
    def __init__(self, repository: FakeExecutionRepository) -> None:
        self.trading = repository
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

    result = execute_reference_paper(request(), broker(), lambda: unit_of_work)

    assert isinstance(result, Success)
    assert unit_of_work.committed is True
    assert result.value == repository.record


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

    result = execute_reference_paper(request(), broker(), lambda: unit_of_work)

    assert isinstance(result, Success)
    assert result.value == repository.record
    assert unit_of_work.committed is False


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
    idempotent = execute_reference_paper(request(), broker(), lambda: idempotent_uow)
    repository.record = None
    repository.intent = repository.intent.model_copy(update={"intent_hash": "f" * 64})
    authority_uow = FakeUnitOfWork(repository)
    authority = execute_reference_paper(request(), broker(), lambda: authority_uow)

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

    result = execute_reference_paper(request(), broker(), lambda: unit_of_work)

    assert isinstance(result, Failure)
    assert unit_of_work.committed is False


def test_sequence_rehydration_and_model_failure_are_fail_closed() -> None:
    sequence_repository = FakeExecutionRepository()
    sequence_repository.account = sequence_repository.account.model_copy(
        update={"account_aggregate_sequence": 9}
    )
    sequence_uow = FakeUnitOfWork(sequence_repository)
    stale = execute_reference_paper(request(), broker(), lambda: sequence_uow)
    model_repository = FakeExecutionRepository()
    model_uow = FakeUnitOfWork(model_repository)
    unavailable = execute_reference_paper(request(), FailingModel(), lambda: model_uow)

    assert isinstance(stale, Failure)
    assert stale.error.code is ErrorCode.CONFLICT
    assert isinstance(unavailable, Failure)
    assert unavailable.error.code is ErrorCode.DATA_UNAVAILABLE
    assert sequence_uow.committed is False
    assert model_uow.committed is False
