"""Clean-room contracts for optional external community platforms.

These contracts deliberately expose research publication and observation only.
External platform state is untrusted evidence and never canonical control-plane state.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from .common import (
    ArtifactRef,
    ContractModel,
    JsonValue,
    NonEmptyString,
    Sha256,
    UnitDecimal,
    UTCDateTime,
)

PublicSummary = Annotated[str, Field(min_length=1, max_length=4_000)]
PublicTitle = Annotated[str, Field(min_length=1, max_length=256)]
PublicContent = Annotated[str, Field(min_length=1, max_length=16_000)]
Cursor = Annotated[str, Field(min_length=1, max_length=1_024)]
IdempotencyKey = Annotated[str, Field(min_length=1, max_length=256)]


class ExternalEvidenceKind(StrEnum):
    COMMUNITY_FEEDBACK = "community_feedback"
    PLATFORM_EVENT = "platform_event"
    REMOTE_POSITION = "remote_position"
    REMOTE_OUTCOME = "remote_outcome"
    REMOTE_PRICE = "remote_price"
    CHALLENGE_RESULT = "challenge_result"
    EXPERIMENT_RESULT = "experiment_result"


class ChallengeAction(StrEnum):
    JOIN = "join"
    SUBMIT_RESEARCH = "submit_research"
    VOTE = "vote"


class ChallengeVote(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REVISE = "revise"


class ExperimentAction(StrEnum):
    ENROLL = "enroll"
    SUBMIT_OBSERVATION = "submit_observation"
    SUBMIT_RESEARCH_RESULT = "submit_research_result"


class ExternalActivityStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    COMPLETED = "completed"


def _artifact_is_bound(reference: str, digest: str) -> bool:
    return reference == f"sha256:{digest}"


class PublishThesisRequest(ContractModel):
    request_id: UUID
    run_id: UUID
    platform: NonEmptyString
    idempotency_key: IdempotencyKey
    subject: NonEmptyString
    market: NonEmptyString
    as_of: UTCDateTime
    public_title: PublicTitle
    public_summary: PublicSummary
    thesis_artifact_ref: ArtifactRef
    thesis_content_hash: Sha256
    evidence_ids: tuple[UUID, ...]
    redaction_policy_version: NonEmptyString
    redaction_manifest_hash: Sha256
    observation_deadline: UTCDateTime
    challenge_ref: str | None = None
    mission_ref: str | None = None
    team_ref: str | None = None
    publication_scope: Literal["public"] = "public"

    @model_validator(mode="after")
    def validate_publication_boundary(self) -> Self:
        if not _artifact_is_bound(self.thesis_artifact_ref, self.thesis_content_hash):
            raise ValueError("thesis artifact must match thesis_content_hash")
        if not self.evidence_ids or len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("evidence_ids must be non-empty and unique")
        if self.observation_deadline <= self.as_of:
            raise ValueError("observation_deadline must be later than as_of")
        return self


class PublishedThesis(ContractModel):
    request_id: UUID
    run_id: UUID
    platform: NonEmptyString
    publication_id: NonEmptyString
    external_ref: NonEmptyString
    thesis_content_hash: Sha256
    published_at: UTCDateTime
    observation_deadline: UTCDateTime
    source_url: str | None = None
    response_artifact_ref: ArtifactRef
    response_content_hash: Sha256
    untrusted_content: Literal[True] = True
    remote_authority: Literal["evidence_only"] = "evidence_only"

    @model_validator(mode="after")
    def validate_external_response(self) -> Self:
        if not _artifact_is_bound(self.response_artifact_ref, self.response_content_hash):
            raise ValueError("response artifact must match response_content_hash")
        if self.published_at > self.observation_deadline:
            raise ValueError("published_at cannot be later than observation_deadline")
        return self


class ExternalEvidence(ContractModel):
    evidence_id: UUID
    platform: NonEmptyString
    external_event_id: NonEmptyString
    subject: NonEmptyString
    kind: ExternalEvidenceKind
    payload: dict[str, JsonValue]
    event_time: UTCDateTime
    published_at: UTCDateTime | None
    available_at: UTCDateTime
    observed_at: UTCDateTime
    as_of: UTCDateTime
    source_url: str | None = None
    content_hash: Sha256
    raw_artifact_ref: ArtifactRef
    author_ref: str | None = None
    reputation: UnitDecimal | None = None
    license_tag: NonEmptyString
    redistribution_tag: NonEmptyString
    ingestion_version: NonEmptyString
    untrusted_content: Literal[True] = True
    remote_authority: Literal["evidence_only"] = "evidence_only"

    @model_validator(mode="after")
    def validate_evidence_boundary(self) -> Self:
        if not _artifact_is_bound(self.raw_artifact_ref, self.content_hash):
            raise ValueError("raw artifact must match content_hash")
        if self.available_at > self.observed_at:
            raise ValueError("available_at cannot be later than observed_at")
        if self.available_at > self.as_of:
            raise ValueError("available_at cannot be later than as_of")
        if self.published_at is not None and self.published_at > self.available_at:
            raise ValueError("published_at cannot be later than available_at")
        if self.reputation is not None and not self.author_ref:
            raise ValueError("reputation requires author_ref")
        return self


class FeedbackPollRequest(ContractModel):
    request_id: UUID
    run_id: UUID
    platform: NonEmptyString
    publication_id: NonEmptyString
    cursor: Cursor | None = None
    limit: int = Field(default=100, ge=1, le=200)
    as_of: UTCDateTime
    deadline: UTCDateTime

    @model_validator(mode="after")
    def validate_deadline(self) -> Self:
        if self.deadline <= self.as_of:
            raise ValueError("deadline must be later than as_of")
        return self


class FeedbackPage(ContractModel):
    request_id: UUID
    run_id: UUID
    platform: NonEmptyString
    publication_id: NonEmptyString
    request_cursor: Cursor | None = None
    next_cursor: Cursor | None = None
    evidence: tuple[ExternalEvidence, ...]
    observed_at: UTCDateTime
    response_artifact_ref: ArtifactRef
    response_content_hash: Sha256
    untrusted_content: Literal[True] = True
    remote_authority: Literal["evidence_only"] = "evidence_only"

    @model_validator(mode="after")
    def validate_feedback_page(self) -> Self:
        if not _artifact_is_bound(self.response_artifact_ref, self.response_content_hash):
            raise ValueError("response artifact must match response_content_hash")
        if any(item.kind is not ExternalEvidenceKind.COMMUNITY_FEEDBACK for item in self.evidence):
            raise ValueError("feedback page may contain only community feedback evidence")
        evidence_ids = [item.evidence_id for item in self.evidence]
        event_ids = [item.external_event_id for item in self.evidence]
        if len(set(evidence_ids)) != len(evidence_ids) or len(set(event_ids)) != len(event_ids):
            raise ValueError("feedback evidence must be deduplicated")
        expected = sorted(
            self.evidence, key=lambda item: (item.available_at, item.external_event_id)
        )
        if list(self.evidence) != expected:
            raise ValueError("feedback evidence must be stably ordered")
        if any(item.observed_at > self.observed_at for item in self.evidence):
            raise ValueError("feedback evidence cannot be observed after the page")
        return self


class _ExternalActivityRequest(ContractModel):
    request_id: UUID
    run_id: UUID
    platform: NonEmptyString
    idempotency_key: IdempotencyKey
    activity_ref: NonEmptyString
    payload_artifact_ref: ArtifactRef
    payload_content_hash: Sha256
    as_of: UTCDateTime
    deadline: UTCDateTime
    research_only: Literal[True] = True

    @model_validator(mode="after")
    def validate_activity_request(self) -> Self:
        if not _artifact_is_bound(self.payload_artifact_ref, self.payload_content_hash):
            raise ValueError("payload artifact must match payload_content_hash")
        if self.deadline <= self.as_of:
            raise ValueError("deadline must be later than as_of")
        return self


class ChallengeRequest(_ExternalActivityRequest):
    action: ChallengeAction
    submission_ref: str | None = None
    vote: ChallengeVote | None = None
    public_content: PublicContent | None = None
    redaction_policy_version: str | None = None
    redaction_manifest_hash: Sha256 | None = None

    @model_validator(mode="after")
    def validate_challenge_action(self) -> Self:
        redaction = (self.redaction_policy_version, self.redaction_manifest_hash)
        if self.action is ChallengeAction.JOIN:
            if any((self.submission_ref, self.vote, self.public_content, *redaction)):
                raise ValueError("challenge join cannot include submission content")
        elif self.action is ChallengeAction.SUBMIT_RESEARCH:
            if not self.public_content or not all(redaction):
                raise ValueError("research submission requires public redacted content")
            if self.submission_ref is not None or self.vote is not None:
                raise ValueError("research submission cannot include a vote")
        elif (
            not self.submission_ref
            or self.vote is None
            or not self.public_content
            or not all(redaction)
        ):
            raise ValueError("challenge vote requires identity, vote and public content")
        return self


class ExperimentRequest(_ExternalActivityRequest):
    action: ExperimentAction
    market: str | None = None
    subject: str | None = None
    public_title: PublicTitle | None = None
    public_content: PublicContent | None = None
    redaction_policy_version: str | None = None
    redaction_manifest_hash: Sha256 | None = None

    @model_validator(mode="after")
    def validate_experiment_action(self) -> Self:
        publication = (
            self.market,
            self.subject,
            self.public_title,
            self.public_content,
            self.redaction_policy_version,
            self.redaction_manifest_hash,
        )
        if self.action is ExperimentAction.ENROLL:
            if any(publication):
                raise ValueError("experiment enrollment cannot publish content")
        elif not all(publication):
            raise ValueError("experiment submission requires public redacted content")
        return self


class _ExternalActivityResult(ContractModel):
    request_id: UUID
    run_id: UUID
    platform: NonEmptyString
    activity_ref: NonEmptyString
    status: ExternalActivityStatus
    external_ref: NonEmptyString
    occurred_at: UTCDateTime
    response_artifact_ref: ArtifactRef
    response_content_hash: Sha256
    untrusted_content: Literal[True] = True
    remote_authority: Literal["evidence_only"] = "evidence_only"

    @model_validator(mode="after")
    def validate_activity_result(self) -> Self:
        if not _artifact_is_bound(self.response_artifact_ref, self.response_content_hash):
            raise ValueError("response artifact must match response_content_hash")
        return self


class ChallengeResult(_ExternalActivityResult):
    action: ChallengeAction


class ExperimentResult(_ExternalActivityResult):
    action: ExperimentAction
