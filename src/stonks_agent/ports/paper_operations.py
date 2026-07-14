"""Typed persistence and transaction boundaries for paper operator actions."""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from stonks_agent.domain.errors import Result
from stonks_agent.domain.ledger import LedgerReconciliationReport
from stonks_agent.domain.operations import (
    ActivateKillSwitchCommand,
    KillSwitchScope,
    PaperKillSwitchState,
    PaperOperationRecord,
    PaperOperatorAction,
    PaperReconciliationResult,
    ReconcilePaperCommand,
    ResumePaperCommand,
    ResumePreparation,
)
from stonks_agent.ports.ledger import LedgerPort


@runtime_checkable
class PaperOperationsPort(Protocol):
    def get_kill_switch(
        self, scope: KillSwitchScope, account_id: str | None
    ) -> Result[PaperKillSwitchState]: ...

    def list_actions(
        self, *, after_sequence: int = 0
    ) -> Result[tuple[PaperOperatorAction, ...]]: ...

    def activate(
        self, command: ActivateKillSwitchCommand, *, actor: str
    ) -> Result[PaperOperationRecord]: ...

    def record_reconciliation(
        self,
        command: ReconcilePaperCommand,
        report: LedgerReconciliationReport,
        *,
        actor: str,
    ) -> Result[PaperReconciliationResult]: ...

    def fail_reconciliation(
        self,
        command: ReconcilePaperCommand,
        *,
        actor: str,
        mismatch_reasons: tuple[str, ...],
    ) -> Result[PaperOperationRecord]: ...

    def prepare_resume(
        self, command: ResumePaperCommand
    ) -> Result[ResumePreparation]: ...

    def complete_resume(
        self,
        command: ResumePaperCommand,
        preparation: ResumePreparation,
        reports: tuple[LedgerReconciliationReport, ...],
        *,
        actor: str,
    ) -> Result[PaperOperationRecord]: ...

    def reject_resume(
        self,
        command: ResumePaperCommand,
        preparation: ResumePreparation,
        *,
        actor: str,
        mismatch_reasons: tuple[str, ...],
    ) -> Result[PaperOperatorAction]: ...


@runtime_checkable
class PaperOperationsUnitOfWork(Protocol):
    operations: PaperOperationsPort
    ledger: LedgerPort

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


type PaperOperationsUnitOfWorkFactory = Callable[[], PaperOperationsUnitOfWork]
