"""Authorization and contract gate in front of the execution port."""

from __future__ import annotations

from pydantic import ValidationError

from stonks_agent.domain.auth import (
    AccessTarget,
    LocalPrincipal,
    Permission,
    ResourceKind,
    authorize,
    authorize_target,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
)
from stonks_agent.domain.fills import ExecutionReceipt
from stonks_agent.domain.orders import ExecutionCommand
from stonks_agent.ports.execution import CanonicalExecutionPort


def submit_paper_execution(
    *,
    principal: LocalPrincipal,
    candidate: object,
    port: CanonicalExecutionPort,
) -> Result[ExecutionReceipt]:
    """Submit one assigned, reservation-bound command to the paper executor."""

    grant = authorize(principal, Permission.EXECUTE_ASSIGNED_PAPER)
    if isinstance(grant, Failure):
        return grant
    if not isinstance(candidate, ExecutionCommand):
        return _invalid_command(candidate)
    try:
        command = ExecutionCommand.model_validate(candidate.model_dump(mode="python"))
        target = AccessTarget(
            kind=ResourceKind.ACCOUNT,
            identifier=command.intent.account_id,
        )
    except ValidationError:
        return _invalid_command(candidate)
    target_grant = authorize_target(
        principal,
        Permission.EXECUTE_ASSIGNED_PAPER,
        target,
    )
    if isinstance(target_grant, Failure):
        return target_grant
    return port.submit(command)


def _invalid_command(candidate: object) -> Failure:
    return Failure(
        StructuredError(
            code=ErrorCode.INVALID_INPUT,
            message="Canonical reservation-bound ExecutionCommand required",
            details={"received_type": type(candidate).__name__},
        )
    )
