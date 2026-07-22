from __future__ import annotations

from decimal import Decimal
from types import TracebackType
from uuid import UUID

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.application.ledger.reconcile import reconcile_paper_account
from stonks_agent.application.ledger.replay import replay_journal
from stonks_agent.domain.artifact import ArtifactMetadata
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.journal import JournalTransaction
from stonks_agent.domain.ledger import LedgerProjection
from stonks_agent.domain.portfolio import AccountPortfolioSnapshot, CashBalance
from stonks_agent.domain.trading_persistence import PaperExecutionRecord
from stonks_contracts.evidence import Sensitivity

from .helpers import NOW

ACCOUNT_ID = "paper-resilience"


def test_corrupted_artifact_is_never_returned_and_trusted_restore_recovers() -> None:
    corrupted = MemoryArtifactStore()
    metadata = ArtifactMetadata(
        media_type="application/json",
        license_tag="Apache-2.0",
        sensitivity=Sensitivity.INTERNAL,
        source="resilience-fixture",
    )
    content = b'{"evidence":"trusted"}'
    finalized = corrupted.finalize(content, metadata=metadata, finalized_at=NOW)
    assert isinstance(finalized, Success)
    content_hash = finalized.value.content_hash

    corrupted._objects[content_hash] = b'{"evidence":"tampered"}'
    denied = corrupted.read(content_hash)

    assert isinstance(denied, Failure)
    assert denied.error.code is ErrorCode.CONFLICT
    restored = MemoryArtifactStore()
    restored_manifest = restored.finalize(content, metadata=metadata, finalized_at=NOW)
    assert isinstance(restored_manifest, Success)
    assert restored_manifest.value.content_hash == content_hash
    assert restored.read(content_hash) == Success(content)


class FaultLedger:
    def __init__(self) -> None:
        self.opening = _opening()
        replayed = replay_journal(self.opening, ())
        assert isinstance(replayed, Success)
        self.truth = replayed.value
        self.projection = LedgerProjection.create(
            account_id=ACCOUNT_ID,
            opening_snapshot_hash=self.truth.opening_snapshot_hash,
            ledger_sequence=1,
            ledger_hash="f" * 64,
            last_occurred_at=NOW,
            balances=(),
            unvalued_instrument_ids=(),
        )
        self.killed = False

    def get_opening_snapshot(self, account_id: str) -> Result[AccountPortfolioSnapshot]:
        assert account_id == ACCOUNT_ID
        return Success(self.opening)

    def list_transactions(
        self,
        account_id: str,
        *,
        after_sequence: int = 0,
    ) -> Result[tuple[JournalTransaction, ...]]:
        assert account_id == ACCOUNT_ID
        assert after_sequence == 0
        return Success(())

    def get_projection(self, account_id: str) -> Result[LedgerProjection]:
        assert account_id == ACCOUNT_ID
        return Success(self.projection)

    def validate_account_graph(self, account_id: str) -> Result[bool]:
        assert account_id == ACCOUNT_ID
        return Success(True)

    def activate_global_kill_switch(
        self,
        *,
        reason_code: str,
        actor: str,
    ) -> Result[bool]:
        assert reason_code == "ledger_reconciliation_failed"
        assert actor == "system:ledger_reconciliation"
        self.killed = True
        return Success(True)

    def execution_enabled(self, account_id: str) -> Result[bool]:
        assert account_id == ACCOUNT_ID
        if self.killed:
            return _failure(ErrorCode.CAPABILITY_DENIED, "kill switch active")
        return Success(True)

    def get_head(self, account_id: str) -> object:
        raise AssertionError("unexpected ledger mutation")

    def append(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("unexpected journal side effect")

    def validate_execution_graph(self, record: PaperExecutionRecord) -> object:
        raise AssertionError("unexpected execution graph access")


class FaultUnitOfWork:
    def __init__(self, ledger: FaultLedger) -> None:
        self.ledger = ledger
        self.trading = _NoTradingSideEffects()
        self.committed = False

    def __enter__(self) -> FaultUnitOfWork:
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


class FaultFactory:
    def __init__(self, ledger: FaultLedger) -> None:
        self.ledger = ledger
        self.transactions: list[FaultUnitOfWork] = []

    def __call__(self) -> FaultUnitOfWork:
        transaction = FaultUnitOfWork(self.ledger)
        self.transactions.append(transaction)
        return transaction


class _NoTradingSideEffects:
    orders_created = 0


def test_ledger_mismatch_activates_kill_switch_and_recovery_stays_closed() -> None:
    ledger = FaultLedger()
    factory = FaultFactory(ledger)

    mismatch = reconcile_paper_account(
        ACCOUNT_ID,
        as_of=NOW,
        unit_of_work=factory,
    )

    assert isinstance(mismatch, Failure)
    assert mismatch.error.code is ErrorCode.CONFLICT
    assert ledger.killed
    assert len(factory.transactions) == 2
    assert not factory.transactions[0].committed
    assert factory.transactions[1].committed
    assert factory.transactions[0].trading.orders_created == 0
    assert isinstance(ledger.execution_enabled(ACCOUNT_ID), Failure)

    ledger.projection = ledger.truth
    healthy = reconcile_paper_account(
        ACCOUNT_ID,
        as_of=NOW,
        unit_of_work=factory,
    )
    assert isinstance(healthy, Success)
    assert healthy.value.matched
    assert ledger.killed
    assert isinstance(ledger.execution_enabled(ACCOUNT_ID), Failure)


def _opening() -> AccountPortfolioSnapshot:
    return AccountPortfolioSnapshot(
        snapshot_id=UUID("69000000-0000-4000-8000-000000000003"),
        account_id=ACCOUNT_ID,
        as_of=NOW,
        account_aggregate_sequence=0,
        portfolio_sequence=0,
        ledger_sequence=0,
        ledger_hash=None,
        cash=(
            CashBalance(
                currency="USD",
                settled_amount=Decimal("1000.00"),
                reserved_amount=Decimal("0.00"),
                quantum=Decimal("0.01"),
            ),
        ),
    )


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
