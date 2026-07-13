"""Artifact-backed, idempotent report delivery contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks_agent.domain.outbox import OutboxAckReceipt
from stonks_contracts.common import NonEmptyString, Sha256, UTCDateTime


class DeliveryChannel(StrEnum):
    CONSOLE = "console"
    FILE = "file"
    EMAIL = "email"
    WEBHOOK = "webhook"


class DeliveryStatus(StrEnum):
    SENT = "sent"
    SKIPPED = "skipped"


class DeliveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    delivery_id: UUID
    report_id: UUID
    channel: DeliveryChannel
    format: NonEmptyString
    content_hash: Sha256
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,255}$")
    required: bool


class DeliveryCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request: DeliveryRequest
    media_type: NonEmptyString
    chunks: tuple[str, ...] = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def validate_chunks(self) -> Self:
        if any(not chunk for chunk in self.chunks):
            raise ValueError("delivery chunks cannot be empty")
        return self


class DeliveryReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    delivery_id: UUID
    report_id: UUID
    channel: DeliveryChannel
    status: DeliveryStatus
    content_hash: Sha256
    idempotency_key: NonEmptyString
    chunk_count: int = Field(ge=0)
    provider_receipt_id: str | None = Field(default=None, max_length=512)
    delivered_at: UTCDateTime
    reason: str | None = Field(default=None, max_length=256)


class DeliveryProcessReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    delivery: DeliveryReceipt
    outbox_ack: OutboxAckReceipt
