"""Shared fail-closed helpers for paper operator transactions."""

from __future__ import annotations

from datetime import datetime

from stonks_agent.application.ledger.reconcile import compare_ledger_projection
from stonks_agent.domain._trading import failure
from stonks_agent.domain.errors import ErrorCode, Failure, Result
from stonks_agent.domain.ledger import LedgerReconciliationReport
from stonks_agent.ports.paper_operations import PaperOperationsUnitOfWork
from stonks_agent.ports.trading_unit_of_work import TradingCommitError


def reconcile_locked_account(
    transaction: PaperOperationsUnitOfWork,
    account_id: str,
    *,
    as_of: datetime,
) -> Result[LedgerReconciliationReport]:
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
    if not graph.value:
        return failure(ErrorCode.CONFLICT, "Paper account graph is invalid")
    return compare_ledger_projection(
        opening.value,
        journals.value,
        projection.value,
        as_of=as_of,
    )


def commit_or_failure(
    transaction: PaperOperationsUnitOfWork,
    *,
    message: str,
) -> Failure | None:
    try:
        transaction.commit()
    except TradingCommitError:
        return failure(ErrorCode.INTERNAL_ERROR, message)
    return None
