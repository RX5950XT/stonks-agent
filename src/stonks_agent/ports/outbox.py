"""Transactional outbox delivery boundary."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable
from uuid import UUID

from stonks_agent.domain.errors import Result
from stonks_agent.domain.outbox import OutboxAckReceipt, OutboxLease


@runtime_checkable
class OutboxPort(Protocol):
    def claim(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_for: timedelta,
        limit: int,
    ) -> Result[tuple[OutboxLease, ...]]: ...

    def ack(
        self,
        outbox_id: UUID,
        *,
        worker_id: str,
        lease_generation: int,
        lease_nonce: UUID,
        now: datetime,
    ) -> Result[OutboxAckReceipt]: ...

    def nack(
        self,
        outbox_id: UUID,
        *,
        worker_id: str,
        lease_generation: int,
        lease_nonce: UUID,
        now: datetime,
        retry_at: datetime,
        error_code: str,
    ) -> Result[bool]: ...
