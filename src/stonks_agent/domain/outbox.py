"""Durable outbox lease and acknowledgement contracts."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from stonks_agent.domain.telemetry import TraceCarrier
from stonks_contracts.common import NonEmptyString, UTCDateTime


class OutboxLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outbox_id: UUID
    aggregate_type: NonEmptyString
    aggregate_id: NonEmptyString
    sequence: int = Field(ge=1)
    topic: NonEmptyString
    payload: dict[str, object]
    idempotency_key: NonEmptyString
    lease_owner: NonEmptyString
    lease_until: UTCDateTime
    lease_generation: int = Field(ge=1)
    lease_nonce: UUID
    attempts: int = Field(ge=1)
    trace_carrier: TraceCarrier | None = None
    correlation_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )


class OutboxAckReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outbox_id: UUID
    worker_id: NonEmptyString
    lease_generation: int = Field(ge=1)
    lease_nonce: UUID
    published_at: UTCDateTime
