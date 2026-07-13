"""Versioned, immutable analysis context assembled from canonical evidence."""

from __future__ import annotations

from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks_contracts.common import (
    NonEmptyString,
    UnitDecimal,
    UTCDateTime,
    stable_payload_hash,
)
from stonks_contracts.evidence import EvidenceItem, EvidenceKind, Sensitivity
from stonks_contracts.market_data import DataQualityStatus


class EvidenceRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    kinds: tuple[EvidenceKind, ...] = Field(min_length=1, max_length=16)
    required: bool
    supported: bool = True
    minimum_items: int = Field(ge=0, le=10_000)
    maximum_items: int = Field(ge=1, le=10_000)
    freshness_seconds: int | None = Field(default=None, ge=0, le=31_536_000)

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if len(self.kinds) != len(set(self.kinds)):
            raise ValueError("evidence requirement kinds must be unique")
        if self.minimum_items > self.maximum_items:
            raise ValueError("minimum_items cannot exceed maximum_items")
        if not self.supported and self.required:
            raise ValueError("unsupported capability cannot be required")
        return self


class AnalysisContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0.0"] = "1.0.0"
    context_id: UUID
    run_id: UUID
    subject: NonEmptyString
    as_of: UTCDateTime
    requirements: tuple[EvidenceRequirement, ...] = Field(min_length=1, max_length=128)
    allowed_sensitivities: tuple[Sensitivity, ...] = Field(min_length=1, max_length=3)
    allowed_license_tags: tuple[NonEmptyString, ...] = Field(
        min_length=1, max_length=128
    )
    allowed_redistribution_tags: tuple[NonEmptyString, ...] = Field(
        min_length=1, max_length=128
    )

    @model_validator(mode="after")
    def validate_unique_policy(self) -> Self:
        collections = (
            tuple(item.capability for item in self.requirements),
            self.allowed_sensitivities,
            self.allowed_license_tags,
            self.allowed_redistribution_tags,
        )
        if any(len(values) != len(set(values)) for values in collections):
            raise ValueError("analysis context policy values must be unique")
        return self


class EvidenceBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: NonEmptyString
    status: DataQualityStatus
    completeness: UnitDecimal
    evidence_refs: tuple[UUID, ...]
    sources: tuple[NonEmptyString, ...]
    latest_available_at: UTCDateTime | None = None
    warnings: tuple[NonEmptyString, ...] = ()
    missing_reason: str | None = None

    @model_validator(mode="after")
    def validate_refs_and_status(self) -> Self:
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("evidence block refs must be unique")
        if len(self.sources) != len(set(self.sources)):
            raise ValueError("evidence block sources must be unique")
        if bool(self.evidence_refs) != (self.latest_available_at is not None):
            raise ValueError("evidence refs require latest_available_at")
        if self.status is DataQualityStatus.AVAILABLE and not self.evidence_refs:
            raise ValueError("available block requires evidence")
        return self


class AnalysisContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0.0"] = "1.0.0"
    context_id: UUID
    run_id: UUID
    subject: NonEmptyString
    as_of: UTCDateTime
    evidence: tuple[EvidenceItem, ...]
    blocks: tuple[EvidenceBlock, ...]
    data_limitations: tuple[NonEmptyString, ...]

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        capabilities = tuple(block.capability for block in self.blocks)
        referenced = {
            evidence_id for block in self.blocks for evidence_id in block.evidence_refs
        }
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("analysis context evidence must be unique")
        if len(capabilities) != len(set(capabilities)):
            raise ValueError("analysis context capabilities must be unique")
        if referenced != set(evidence_ids):
            raise ValueError("analysis context block refs must exactly cover evidence")
        if any(
            item.subject != self.subject
            or item.available_at > self.as_of
            or item.as_of > self.as_of
            for item in self.evidence
        ):
            raise ValueError("analysis context evidence exceeds scope")
        return self

    @property
    def payload_hash(self) -> str:
        return stable_payload_hash(self.model_dump(mode="json"))
