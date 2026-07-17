"""Daily ledger replay and fail-closed PostgreSQL projection reconciliation."""

from __future__ import annotations

from datetime import datetime

from stonks_agent.application.ledger.replay import replay_journal
from stonks_agent.application.telemetry import record_operation
from stonks_agent.domain._trading import failure
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_agent.domain.journal import JournalTransaction
from stonks_agent.domain.ledger import LedgerProjection, LedgerReconciliationReport
from stonks_agent.domain.portfolio import AccountPortfolioSnapshot
from stonks_agent.domain.telemetry import ComponentName, OperationName
from stonks_agent.ports.telemetry import OperationRecorderPort
from stonks_agent.ports.trading_unit_of_work import (
    TradingCommitError,
    TradingUnitOfWorkFactory,
)


def compare_ledger_projection(
    opening: AccountPortfolioSnapshot,
    transactions: tuple[JournalTransaction, ...],
    database_projection: LedgerProjection,
    *,
    as_of: datetime,
) -> Result[LedgerReconciliationReport]:
    replayed = replay_journal(opening, transactions)
    if isinstance(replayed, Failure):
        return replayed
    reasons: list[str] = []
    if replayed.value.ledger_sequence != database_projection.ledger_sequence:
        reasons.append("ledger_sequence_mismatch")
    if replayed.value.ledger_hash != database_projection.ledger_hash:
        reasons.append("ledger_hash_mismatch")
    if replayed.value.projection_hash != database_projection.projection_hash:
        reasons.append("projection_hash_mismatch")
    try:
        return Success(
            LedgerReconciliationReport(
                account_id=opening.account_id,
                as_of=as_of,
                ledger_sequence=replayed.value.ledger_sequence,
                replay_projection_hash=replayed.value.projection_hash,
                database_projection_hash=database_projection.projection_hash,
                matched=not reasons,
                mismatch_reasons=tuple(sorted(reasons)),
            )
        )
    except ValueError:
        return failure(ErrorCode.CONFLICT, "Ledger reconciliation report is invalid")


def reconcile_paper_account(
    account_id: str,
    *,
    as_of: datetime,
    unit_of_work: TradingUnitOfWorkFactory,
    telemetry: OperationRecorderPort | None = None,
) -> Result[LedgerReconciliationReport]:
    return record_operation(
        telemetry,
        component=ComponentName.RECONCILIATION,
        operation=OperationName.RECONCILE,
        call=lambda: _reconcile_paper_account(
            account_id,
            as_of=as_of,
            unit_of_work=unit_of_work,
        ),
    )


def _reconcile_paper_account(
    account_id: str,
    *,
    as_of: datetime,
    unit_of_work: TradingUnitOfWorkFactory,
) -> Result[LedgerReconciliationReport]:
    result = _reconcile_once(account_id, as_of=as_of, unit_of_work=unit_of_work)
    if isinstance(result, Success) and result.value.matched:
        return result
    activated = _activate_reconciliation_kill_switch(unit_of_work)
    if isinstance(activated, Failure):
        return activated
    if isinstance(result, Failure):
        return result
    return failure(ErrorCode.CONFLICT, "Ledger reconciliation mismatch")


def _reconcile_once(
    account_id: str,
    *,
    as_of: datetime,
    unit_of_work: TradingUnitOfWorkFactory,
) -> Result[LedgerReconciliationReport]:
    with unit_of_work() as transaction:
        opening = transaction.ledger.get_opening_snapshot(account_id)
        if isinstance(opening, Failure):
            return opening
        journals = transaction.ledger.list_transactions(account_id)
        if isinstance(journals, Failure):
            return journals
        projection = transaction.ledger.get_projection(account_id)
        if isinstance(projection, Failure):
            return projection
        graph = transaction.ledger.validate_account_graph(account_id)
        if isinstance(graph, Failure):
            return graph
        return compare_ledger_projection(
            opening.value,
            journals.value,
            projection.value,
            as_of=as_of,
        )


def _activate_reconciliation_kill_switch(
    unit_of_work: TradingUnitOfWorkFactory,
) -> Result[bool]:
    with unit_of_work() as transaction:
        activated = transaction.ledger.activate_global_kill_switch(
            reason_code="ledger_reconciliation_failed",
            actor="system:ledger_reconciliation",
        )
        if isinstance(activated, Failure):
            return failure(
                ErrorCode.INTERNAL_ERROR,
                "Reconciliation failed and kill switch activation failed",
            )
        try:
            transaction.commit()
        except TradingCommitError:
            return failure(
                ErrorCode.INTERNAL_ERROR,
                "Reconciliation kill switch activation did not commit",
            )
        return activated
