"""Typed execution boundary accepting canonical commands only."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result
from stonks_contracts.execution import ExecutionCommand, ExecutionReceipt


@runtime_checkable
class ExecutionPort(Protocol):
    """Submit a risk-approved, reservation-backed execution command."""

    def submit(self, command: ExecutionCommand) -> Result[ExecutionReceipt]: ...
