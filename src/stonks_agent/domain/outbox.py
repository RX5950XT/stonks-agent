"""Durable outbox lease and acknowledgement contracts."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

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
    attempts: int = Field(ge=1)


class OutboxAckReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outbox_id: UUID
    worker_id: NonEmptyString
    published_at: UTCDateTime
