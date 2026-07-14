"""Local contracts for the clean-room AI-Trader HTTP adapter."""

from __future__ import annotations

from threading import RLock
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_contracts.common import (
    ArtifactRef,
    ContractModel,
    NonEmptyString,
    Sha256,
    UTCDateTime,
)
from stonks_contracts.platform import IdempotencyKey


class AiTraderReplyRequest(ContractModel):
    request_id: UUID
    run_id: UUID
    idempotency_key: IdempotencyKey
    publication_id: NonEmptyString
    public_content: str = Field(min_length=1, max_length=16_000)
    redaction_policy_version: NonEmptyString
    redaction_manifest_hash: Sha256
    as_of: UTCDateTime
    deadline: UTCDateTime

    @model_validator(mode="after")
    def validate_deadline(self) -> Self:
        if self.deadline <= self.as_of:
            raise ValueError("deadline must be later than as_of")
        return self


class AiTraderWriteReceipt(ContractModel):
    request_id: UUID
    run_id: UUID
    external_ref: NonEmptyString
    occurred_at: UTCDateTime
    response_artifact_ref: ArtifactRef
    response_content_hash: Sha256
    untrusted_content: Literal[True] = True
    remote_authority: Literal["evidence_only"] = "evidence_only"

    @model_validator(mode="after")
    def validate_artifact(self) -> Self:
        if self.response_artifact_ref != f"sha256:{self.response_content_hash}":
            raise ValueError("response artifact must match response_content_hash")
        return self


class AiTraderEventPollRequest(ContractModel):
    request_id: UUID
    run_id: UUID
    cursor: str | None = Field(default=None, max_length=1_024)
    as_of: UTCDateTime
    deadline: UTCDateTime

    @model_validator(mode="after")
    def validate_deadline(self) -> Self:
        if self.deadline <= self.as_of:
            raise ValueError("deadline must be later than as_of")
        return self


class AiTraderEvent(ContractModel):
    event_id: NonEmptyString
    kind: Literal["message", "task"]
    payload: dict[str, object]
    occurred_at: UTCDateTime
    content_hash: Sha256
    raw_artifact_ref: ArtifactRef
    untrusted_content: Literal[True] = True
    remote_authority: Literal["evidence_only"] = "evidence_only"

    @model_validator(mode="after")
    def validate_artifact(self) -> Self:
        if self.raw_artifact_ref != f"sha256:{self.content_hash}":
            raise ValueError("raw artifact must match content_hash")
        return self


class AiTraderEventPage(ContractModel):
    request_id: UUID
    run_id: UUID
    request_cursor: str | None = None
    next_cursor: NonEmptyString
    events: tuple[AiTraderEvent, ...]
    observed_at: UTCDateTime
    response_artifact_ref: ArtifactRef
    response_content_hash: Sha256
    untrusted_content: Literal[True] = True
    remote_authority: Literal["evidence_only"] = "evidence_only"

    @model_validator(mode="after")
    def validate_page(self) -> Self:
        if self.response_artifact_ref != f"sha256:{self.response_content_hash}":
            raise ValueError("response artifact must match response_content_hash")
        ids = [event.event_id for event in self.events]
        if len(set(ids)) != len(ids):
            raise ValueError("events must be deduplicated")
        return self


class MemoryPlatformEventInbox:
    """Thread-safe test/local deduplicator implementing the injected inbox port."""

    def __init__(self) -> None:
        self._hashes: dict[str, str] = {}
        self._lock = RLock()

    def accept(self, event_id: str, payload_hash: str) -> Result[bool]:
        with self._lock:
            existing = self._hashes.get(event_id)
            if existing is None:
                self._hashes[event_id] = payload_hash
                return Success(True)
            if existing == payload_hash:
                return Success(False)
            return Failure(
                StructuredError(
                    code=ErrorCode.CONFLICT,
                    message="Platform event payload conflicts",
                )
            )


class _TolerantModel(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)


class _PublicationResponse(_TolerantModel):
    success: Literal[True]
    signal_id: int = Field(gt=0)


class _WriteResponse(_TolerantModel):
    success: Literal[True]


class _ReplyRecord(_TolerantModel):
    id: int = Field(gt=0)
    signal_id: int = Field(gt=0)
    agent_id: int = Field(gt=0)
    agent_name: str | None = Field(default=None, max_length=256)
    content: str = Field(min_length=1, max_length=16_000)
    created_at: UTCDateTime
    accepted: bool = False
    agent_is_verified: bool = False


class _RepliesResponse(_TolerantModel):
    replies: tuple[_ReplyRecord, ...]


class _HeartbeatRecord(_TolerantModel):
    id: int = Field(gt=0)
    type: str = Field(min_length=1, max_length=256)
    content: str | None = Field(default=None, max_length=16_000)
    data: dict[str, object] | None = None
    input_data: dict[str, object] | None = None
    created_at: UTCDateTime


class _HeartbeatResponse(_TolerantModel):
    agent_id: int = Field(gt=0)
    server_time: UTCDateTime
    messages: tuple[_HeartbeatRecord, ...]
    tasks: tuple[_HeartbeatRecord, ...]
    message_count: int = Field(ge=0)
    task_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.message_count != len(self.messages) or self.task_count != len(
            self.tasks
        ):
            raise ValueError("heartbeat counts do not match event arrays")
        return self


class _Participant(_TolerantModel):
    id: int = Field(gt=0)


class _ChallengeJoinResponse(_TolerantModel):
    joined: bool
    idempotent: bool
    participant: _Participant


class _ChallengeSubmissionResponse(_TolerantModel):
    id: int = Field(gt=0)


class _VoteRecord(_TolerantModel):
    id: int = Field(gt=0)


class _ChallengeVoteResponse(_TolerantModel):
    vote: _VoteRecord


class _ExperimentAssignResponse(_TolerantModel):
    experiment_key: str = Field(min_length=1, max_length=128)
    variant_key: str = Field(min_length=1, max_length=128)
    idempotent: bool
