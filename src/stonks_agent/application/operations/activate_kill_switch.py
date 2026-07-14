"""Activate and inspect the audited paper kill switch."""

from __future__ import annotations

from stonks_agent.application.operations._common import commit_or_failure
from stonks_agent.domain.auth import LocalPrincipal, Permission, authorize
from stonks_agent.domain.errors import Failure, Result
from stonks_agent.domain.operations import (
    ActivateKillSwitchCommand,
    KillSwitchScope,
    PaperKillSwitchState,
    PaperOperationRecord,
    PaperOperatorAction,
)
from stonks_agent.ports.paper_operations import PaperOperationsUnitOfWorkFactory


def activate_kill_switch(
    principal: LocalPrincipal,
    command: ActivateKillSwitchCommand,
    unit_of_work: PaperOperationsUnitOfWorkFactory,
) -> Result[PaperOperationRecord]:
    granted = authorize(principal, Permission.OPERATE_PAPER)
    if isinstance(granted, Failure):
        return granted
    with unit_of_work() as transaction:
        result = transaction.operations.activate(command, actor=principal.subject)
        if isinstance(result, Failure):
            return result
        failed = commit_or_failure(
            transaction, message="Kill switch activation did not commit"
        )
        return failed or result


def read_kill_switch(
    principal: LocalPrincipal,
    scope: KillSwitchScope,
    account_id: str | None,
    unit_of_work: PaperOperationsUnitOfWorkFactory,
) -> Result[PaperKillSwitchState]:
    granted = authorize(principal, Permission.OPERATE_PAPER)
    if isinstance(granted, Failure):
        return granted
    with unit_of_work() as transaction:
        return transaction.operations.get_kill_switch(scope, account_id)


def read_operator_actions(
    principal: LocalPrincipal,
    *,
    after_sequence: int,
    unit_of_work: PaperOperationsUnitOfWorkFactory,
) -> Result[tuple[PaperOperatorAction, ...]]:
    granted = authorize(principal, Permission.OPERATE_PAPER)
    if isinstance(granted, Failure):
        return granted
    with unit_of_work() as transaction:
        return transaction.operations.list_actions(after_sequence=after_sequence)
