"""Run and audit one operator-requested paper account reconciliation."""

from __future__ import annotations

from stonks_agent.application.operations._common import (
    commit_or_failure,
    reconcile_locked_account,
)
from stonks_agent.domain._trading import failure
from stonks_agent.domain.auth import LocalPrincipal, Permission, authorize
from stonks_agent.domain.errors import ErrorCode, Failure, Result
from stonks_agent.domain.ledger import LedgerReconciliationReport
from stonks_agent.domain.operations import (
    PaperReconciliationResult,
    ReconcilePaperCommand,
)
from stonks_agent.ports.paper_operations import PaperOperationsUnitOfWorkFactory


def reconcile_paper_state(
    principal: LocalPrincipal,
    command: ReconcilePaperCommand,
    unit_of_work: PaperOperationsUnitOfWorkFactory,
) -> Result[PaperReconciliationResult]:
    granted = authorize(principal, Permission.OPERATE_PAPER)
    if isinstance(granted, Failure):
        return granted
    with unit_of_work() as transaction:
        report = reconcile_locked_account(
            transaction,
            command.account_id,
            as_of=command.requested_at,
        )
        if isinstance(report, Failure) or not report.value.matched:
            reasons = _failure_reasons(report)
            activated = transaction.operations.fail_reconciliation(
                command,
                actor=principal.subject,
                mismatch_reasons=reasons,
            )
            if isinstance(activated, Failure):
                return activated
            commit_failure = commit_or_failure(
                transaction,
                message="Failed reconciliation safety action did not commit",
            )
            if commit_failure is not None:
                return commit_failure
            return failure(
                ErrorCode.CONFLICT,
                "Paper account reconciliation failed",
                action_id=str(activated.value.action.action_id),
                mismatch_reasons=reasons,
            )
        recorded = transaction.operations.record_reconciliation(
            command,
            report.value,
            actor=principal.subject,
        )
        if isinstance(recorded, Failure):
            return recorded
        commit_failure = commit_or_failure(
            transaction,
            message="Reconciliation audit did not commit",
        )
        return commit_failure or recorded


def _failure_reasons(
    report: Result[LedgerReconciliationReport],
) -> tuple[str, ...]:
    if isinstance(report, Failure):
        return (f"reconciliation_{report.error.code.value}",)
    return tuple(sorted(report.value.mismatch_reasons))
