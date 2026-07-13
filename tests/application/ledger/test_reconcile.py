from __future__ import annotations

from types import TracebackType

import pytest

from stonks_agent.application.ledger.post import build_fill_journal
from stonks_agent.application.ledger.reconcile import (
    compare_ledger_projection,
    reconcile_paper_account,
)
from stonks_agent.application.ledger.replay import replay_journal
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.journal import JournalTransaction
from stonks_agent.domain.ledger import LedgerProjection
from stonks_agent.domain.portfolio import AccountPortfolioSnapshot
from stonks_agent.domain.trading_persistence import PaperExecutionRecord
from stonks_agent.ports.trading_unit_of_work import TradingCommitError

from .helpers import ACCOUNT_ID, NOW, fill, opening, policy


def _failure() -> Failure:
    return Failure(
        StructuredError(code=ErrorCode.CONFLICT, message="authoritative ledger failed")
    )


class StubLedger:
    def __init__(self, *, fail_at: str | None = None) -> None:
        self.fail_at = fail_at
        self.killed = False
        self.opening = opening()
        replayed = replay_journal(self.opening, ())
        assert isinstance(replayed, Success)
        self.projection = replayed.value

    def get_opening_snapshot(self, account_id: str) -> Result[AccountPortfolioSnapshot]:
        return _failure() if self.fail_at == "opening" else Success(self.opening)

    def list_transactions(
        self, account_id: str, *, after_sequence: int = 0
    ) -> Result[tuple[JournalTransaction, ...]]:
        return _failure() if self.fail_at == "transactions" else Success(())

    def get_projection(self, account_id: str) -> Result[LedgerProjection]:
        return _failure() if self.fail_at == "projection" else Success(self.projection)

    def validate_account_graph(self, account_id: str) -> Result[bool]:
        return _failure() if self.fail_at == "graph" else Success(True)

    def activate_global_kill_switch(
        self, *, reason_code: str, actor: str
    ) -> Result[bool]:
        if self.fail_at == "kill":
            return _failure()
        assert reason_code == "ledger_reconciliation_failed"
        assert actor == "system:ledger_reconciliation"
        self.killed = True
        return Success(True)

    def get_head(self, account_id: str) -> object:
        raise AssertionError("unused")

    def append(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("unused")

    def validate_execution_graph(self, record: PaperExecutionRecord) -> object:
        raise AssertionError("unused")

    def execution_enabled(self, account_id: str) -> object:
        raise AssertionError("unused")


class StubUnitOfWork:
    def __init__(self, ledger: StubLedger, *, commit_fails: bool = False) -> None:
        self.ledger = ledger
        self.trading = object()
        self.commit_fails = commit_fails
        self.committed = False

    def __enter__(self) -> StubUnitOfWork:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def commit(self) -> None:
        if self.commit_fails:
            raise TradingCommitError("commit failed")
        self.committed = True

    def rollback(self) -> None:
        self.committed = False


class StubFactory:
    def __init__(self, ledger: StubLedger, *, kill_commit_fails: bool = False) -> None:
        self.ledger = ledger
        self.kill_commit_fails = kill_commit_fails
        self.transactions: list[StubUnitOfWork] = []

    def __call__(self) -> StubUnitOfWork:
        transaction = StubUnitOfWork(
            self.ledger,
            commit_fails=self.kill_commit_fails and bool(self.transactions),
        )
        self.transactions.append(transaction)
        return transaction


def test_compare_reports_sequence_hash_and_projection_mismatches() -> None:
    replayed = replay_journal(opening(), ())
    assert isinstance(replayed, Success)
    drifted = LedgerProjection.create(
        account_id=ACCOUNT_ID,
        opening_snapshot_hash=replayed.value.opening_snapshot_hash,
        ledger_sequence=1,
        ledger_hash="a" * 64,
        last_occurred_at=replayed.value.last_occurred_at,
        balances=replayed.value.balances,
        unvalued_instrument_ids=(),
    )

    result = compare_ledger_projection(opening(), (), drifted, as_of=NOW)

    assert isinstance(result, Success)
    assert result.value.mismatch_reasons == (
        "ledger_hash_mismatch",
        "ledger_sequence_mismatch",
        "projection_hash_mismatch",
    )


def test_compare_propagates_replay_failure() -> None:
    initial = replay_journal(opening(), ())
    assert isinstance(initial, Success)
    posted = build_fill_journal(fill(), initial.value, policy())
    assert isinstance(posted, Success)
    gap = posted.value.model_copy(update={"sequence": 2})

    result = compare_ledger_projection(opening(), (gap,), initial.value, as_of=NOW)

    assert isinstance(result, Failure)


@pytest.mark.parametrize("fail_at", ["opening", "transactions", "projection", "graph"])
def test_reconcile_read_or_graph_failure_uses_separate_kill_transaction(
    fail_at: str,
) -> None:
    ledger = StubLedger(fail_at=fail_at)
    factory = StubFactory(ledger)

    result = reconcile_paper_account(ACCOUNT_ID, as_of=NOW, unit_of_work=factory)

    assert isinstance(result, Failure)
    assert ledger.killed
    assert len(factory.transactions) == 2
    assert not factory.transactions[0].committed
    assert factory.transactions[1].committed


def test_reconcile_returns_internal_error_when_kill_activation_fails() -> None:
    ledger = StubLedger(fail_at="kill")
    factory = StubFactory(ledger)
    ledger.projection = ledger.projection.model_copy(
        update={"projection_hash": "a" * 64}
    )

    result = reconcile_paper_account(ACCOUNT_ID, as_of=NOW, unit_of_work=factory)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.INTERNAL_ERROR
    assert not factory.transactions[1].committed


def test_reconcile_returns_internal_error_when_kill_commit_fails() -> None:
    ledger = StubLedger(fail_at="graph")
    factory = StubFactory(ledger, kill_commit_fails=True)

    result = reconcile_paper_account(ACCOUNT_ID, as_of=NOW, unit_of_work=factory)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.INTERNAL_ERROR
    assert ledger.killed
    assert not factory.transactions[1].committed
