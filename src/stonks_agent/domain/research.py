"""Evidence-scoped research, opinion, and structured LLM contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stonks_agent.domain.usage_budget import UsageBudget, UsageConsumption
from stonks_contracts.common import (
    ArtifactRef,
    ConfidenceCalibration,
    UnitDecimal,
    UTCDateTime,
    canonical_json,
)


class ResearchClaimKind(StrEnum):
    EVIDENCED = "evidenced"
    HYPOTHESIS = "hypothesis"


class OpinionRating(StrEnum):
    BULLISH = "bullish"
    NEUTRAL = "neutral"
    BEARISH = "bearish"


class LLMRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class ResearchClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: UUID
    kind: ResearchClaimKind
    text: str = Field(min_length=1, max_length=4_000)
    evidence_ids: frozenset[UUID] = Field(default_factory=frozenset, max_length=128)

    @model_validator(mode="after")
    def require_citation_for_evidenced_claim(self) -> Self:
        if self.kind is ResearchClaimKind.EVIDENCED and not self.evidence_ids:
            raise ValueError("evidenced research claims require citations")
        return self


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    run_id: UUID
    instrument_ids: frozenset[str] = Field(min_length=1, max_length=64)
    as_of: UTCDateTime
    horizon_days: int = Field(ge=1, le=3_650)
    question: str = Field(min_length=1, max_length=4_000)
    allowed_evidence_ids: frozenset[UUID] = Field(min_length=1, max_length=10_000)
    tool_policy_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    model_policy_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    budget: UsageBudget
    created_at: UTCDateTime
    deadline_at: UTCDateTime

    @model_validator(mode="after")
    def validate_deadline(self) -> Self:
        if self.deadline_at <= self.created_at:
            raise ValueError("research deadline must be later than creation time")
        return self


class ResearchArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: UUID
    request_id: UUID
    run_id: UUID
    instrument_ids: frozenset[str] = Field(min_length=1, max_length=64)
    as_of: UTCDateTime
    allowed_evidence_ids: frozenset[UUID] = Field(min_length=1, max_length=10_000)
    claims: tuple[ResearchClaim, ...] = Field(min_length=1, max_length=256)
    counterarguments: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    risks: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    warnings: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    raw_output_artifact_ref: ArtifactRef
    producer: str = Field(min_length=1, max_length=128)
    producer_version: str = Field(min_length=1, max_length=128)
    model_versions: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    tool_versions: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    usage: UsageConsumption
    created_at: UTCDateTime

    @model_validator(mode="after")
    def validate_citation_scope(self) -> Self:
        cited = frozenset(
            evidence_id for claim in self.claims for evidence_id in claim.evidence_ids
        )
        if not cited <= self.allowed_evidence_ids:
            raise ValueError("research artifact cites evidence outside request scope")
        return self


class AgentOpinion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    opinion_id: UUID
    artifact_id: UUID
    instrument_id: str = Field(min_length=1, max_length=128)
    as_of: UTCDateTime
    horizon_days: int = Field(ge=1, le=3_650)
    rating: OpinionRating
    thesis: str = Field(min_length=1, max_length=8_000)
    confidence: UnitDecimal
    confidence_calibration: ConfidenceCalibration
    evidence_ids: frozenset[UUID] = Field(min_length=1, max_length=128)
    producer: str = Field(min_length=1, max_length=128)
    producer_version: str = Field(min_length=1, max_length=128)
    model_versions: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    created_at: UTCDateTime


class UntrustedContentBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_ref: ArtifactRef
    content: str = Field(min_length=1, max_length=32_768)
    untrusted_content: Literal[True] = True


class LLMMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: LLMRole
    content: str = Field(min_length=1, max_length=32_768)


class StructuredLLMRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
    messages: tuple[LLMMessage, ...] = Field(min_length=1, max_length=64)
    untrusted_blocks: tuple[UntrustedContentBlock, ...] = Field(
        default_factory=tuple,
        max_length=128,
    )
    output_schema_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    output_schema_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    output_schema: dict[str, object]
    max_output_tokens: int = Field(ge=1, le=1_000_000)
    deadline_at: UTCDateTime

    @field_validator("output_schema")
    @classmethod
    def validate_schema_size(cls, value: dict[str, object]) -> dict[str, object]:
        if _json_size(value, label="structured output schema") > 65_536:
            raise ValueError("structured output schema exceeds size limit")
        return value


class StructuredLLMResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    model: str = Field(min_length=1, max_length=256)
    parsed_output: dict[str, object]
    raw_output_artifact_ref: ArtifactRef
    usage: UsageConsumption
    created_at: UTCDateTime

    @field_validator("parsed_output")
    @classmethod
    def validate_output_size(cls, value: dict[str, object]) -> dict[str, object]:
        if _json_size(value, label="structured LLM output") > 1_048_576:
            raise ValueError("structured LLM output exceeds size limit")
        return value


def _json_size(value: dict[str, object], *, label: str) -> int:
    try:
        return len(canonical_json(value).encode("utf-8"))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must contain JSON values only") from error
