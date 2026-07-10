"""Authorization and contract gate in front of the execution port."""

from __future__ import annotations

from stonks_agent.domain.auth import LocalPrincipal, Permission, authorize
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
)
from stonks_agent.ports.execution import ExecutionPort
from stonks_contracts.execution import ExecutionCommand, ExecutionReceipt


def submit_paper_execution(
    *,
    principal: LocalPrincipal,
    candidate: object,
    port: ExecutionPort,
) -> Result[ExecutionReceipt]:
    """Fail closed before non-command objects can invoke the execution port."""

    grant = authorize(principal, Permission.OPERATE_PAPER)
    if isinstance(grant, Failure):
        return grant
    if not isinstance(candidate, ExecutionCommand):
        return Failure(
            StructuredError(
                code=ErrorCode.INVALID_INPUT,
                message="Canonical ExecutionCommand required",
                details={"received_type": type(candidate).__name__},
            )
        )
    return port.submit(candidate)
