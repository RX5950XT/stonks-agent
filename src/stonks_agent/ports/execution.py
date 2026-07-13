"""Typed execution boundary accepting canonical commands only."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result
from stonks_agent.domain.fills import ExecutionReceipt as DomainExecutionReceipt
from stonks_agent.domain.orders import ExecutionCommand as DomainExecutionCommand
from stonks_contracts.execution import (
    ExecutionCommand as WireExecutionCommand,
)
from stonks_contracts.execution import (
    ExecutionReceipt as WireExecutionReceipt,
)


@runtime_checkable
class ExecutionPort(Protocol):
    """P0 wire-compatible execution boundary."""

    def submit(self, command: WireExecutionCommand) -> Result[WireExecutionReceipt]: ...


@runtime_checkable
class CanonicalExecutionPort(Protocol):
    """Submit only a P4 reservation-backed canonical paper command."""

    def submit(
        self, command: DomainExecutionCommand
    ) -> Result[DomainExecutionReceipt]: ...

    def get_receipt(
        self,
        *,
        account_id: str,
        idempotency_key: str,
    ) -> Result[DomainExecutionReceipt]: ...
