"""Durable job, lease, and fenced completion contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks_contracts.common import (
    NonEmptyString,
    Sha256,
    UTCDateTime,
    stable_payload_hash,
)


class JobStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    DEAD_LETTER = "dead_letter"


class EnqueueJob(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    run_id: UUID
    job_type: NonEmptyString
    payload: dict[str, object]
    idempotency_key: NonEmptyString
    not_before: UTCDateTime
    deadline_at: UTCDateTime
    max_attempts: int = Field(ge=1, le=100)
    created_at: UTCDateTime

    @model_validator(mode="after")
    def validate_deadline(self) -> Self:
        if self.deadline_at <= self.not_before:
            raise ValueError("deadline_at must be later than not_before")
        return self

    @property
    def payload_hash(self) -> str:
        return stable_payload_hash(self.payload)


class JobRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    run_id: UUID
    job_type: NonEmptyString
    payload: dict[str, object]
    payload_hash: Sha256
    status: JobStatus
    idempotency_key: NonEmptyString
    not_before: UTCDateTime
    deadline_at: UTCDateTime
    attempts: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    attempt_generation: int = Field(ge=0)
    created_at: UTCDateTime
    updated_at: UTCDateTime


class JobLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    run_id: UUID
    job_type: NonEmptyString
    payload: dict[str, object]
    attempt_generation: int = Field(ge=1)
    attempt_nonce: NonEmptyString
    lease_owner: NonEmptyString
    lease_until: UTCDateTime
    attempts: int = Field(ge=1)
    deadline_at: UTCDateTime


class CompleteJob(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    worker_id: NonEmptyString
    attempt_generation: int = Field(ge=1)
    attempt_nonce: NonEmptyString
    result_artifact_hash: Sha256


class JobCompletionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    run_id: UUID
    event_id: UUID
    outbox_id: UUID
    sequence: int = Field(ge=1)
    result_artifact_hash: Sha256
    completed_at: UTCDateTime
