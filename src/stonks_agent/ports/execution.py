"""Typed execution boundary accepting canonical commands only."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result
from stonks_agent.domain.execution_model import (
    PaperExecutionOutcome,
    PaperExecutionRequest,
)
from stonks_agent.domain.fills import ExecutionReceipt as DomainExecutionReceipt
from stonks_agent.domain.orders import ExecutionCommand as DomainExecutionCommand


@runtime_checkable
class CanonicalExecutionPort(Protocol):
    """Sole execution boundary for P4 reservation-backed paper commands."""

    def submit(
        self, command: DomainExecutionCommand
    ) -> Result[DomainExecutionReceipt]: ...


@runtime_checkable
class PaperExecutionModelPort(Protocol):
    """Pure deterministic simulation; transaction ownership stays in core."""

    def execute(
        self, request: PaperExecutionRequest
    ) -> Result[PaperExecutionOutcome]: ...

    def get_receipt(
        self,
        *,
        account_id: str,
        idempotency_key: str,
    ) -> Result[DomainExecutionReceipt]: ...
