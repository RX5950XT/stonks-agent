"""Resume paper execution only after locked reconciliation succeeds."""

from __future__ import annotations

from datetime import datetime

from stonks_agent.application.operations._common import (
    commit_or_failure,
    reconcile_locked_account,
)
from stonks_agent.domain._trading import failure
from stonks_agent.domain.auth import LocalPrincipal, Permission, authorize
from stonks_agent.domain.errors import ErrorCode, Failure, Result
from stonks_agent.domain.ledger import LedgerReconciliationReport
from stonks_agent.domain.operations import PaperOperationRecord, ResumePaperCommand
from stonks_agent.ports.paper_operations import (
    PaperOperationsUnitOfWork,
    PaperOperationsUnitOfWorkFactory,
)


def resume_paper(
    principal: LocalPrincipal,
    command: ResumePaperCommand,
    unit_of_work: PaperOperationsUnitOfWorkFactory,
) -> Result[PaperOperationRecord]:
    granted = authorize(principal, Permission.OPERATE_PAPER)
    if isinstance(granted, Failure):
        return granted
    with unit_of_work() as transaction:
        prepared = transaction.operations.prepare_resume(command)
        if isinstance(prepared, Failure):
            return prepared
        reports, reasons = _reconcile_prepared(
            transaction,
            prepared.value.account_ids,
            as_of=command.requested_at,
        )
        if reasons:
            rejected = transaction.operations.reject_resume(
                command,
                prepared.value,
                actor=principal.subject,
                mismatch_reasons=reasons,
            )
            if isinstance(rejected, Failure):
                return rejected
            commit_failure = commit_or_failure(
                transaction, message="Rejected resume audit did not commit"
            )
            if commit_failure is not None:
                return commit_failure
            return failure(
                ErrorCode.CONFLICT,
                "Paper resume reconciliation failed",
                action_id=str(rejected.value.action_id),
                mismatch_reasons=reasons,
            )
        resumed = transaction.operations.complete_resume(
            command,
            prepared.value,
            reports,
            actor=principal.subject,
        )
        if isinstance(resumed, Failure):
            return resumed
        commit_failure = commit_or_failure(
            transaction, message="Paper resume did not commit"
        )
        return commit_failure or resumed


def _reconcile_prepared(
    transaction: PaperOperationsUnitOfWork,
    account_ids: tuple[str, ...],
    *,
    as_of: datetime,
) -> tuple[tuple[LedgerReconciliationReport, ...], tuple[str, ...]]:
    reports: list[LedgerReconciliationReport] = []
    reasons: list[str] = []
    for account_id in account_ids:
        report = reconcile_locked_account(
            transaction,
            account_id,
            as_of=as_of,
        )
        if isinstance(report, Failure):
            reasons.append(f"{account_id}:{report.error.code.value}")
        elif not report.value.matched:
            reasons.extend(
                f"{account_id}:{reason}" for reason in report.value.mismatch_reasons
            )
        else:
            reports.append(report.value)
    return tuple(reports), tuple(sorted(set(reasons)))
