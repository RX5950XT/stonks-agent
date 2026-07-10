"""Research-only outputs with no portfolio or execution authority."""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from .common import (
    ArtifactRef,
    ConfidenceCalibration,
    ContractModel,
    ModelUsage,
    NonEmptyString,
    UnitDecimal,
    UTCDateTime,
)


class ResearchRequest(ContractModel):
    request_id: UUID
    question: NonEmptyString
    instrument_id: UUID | None = None
    as_of: UTCDateTime
    horizon: NonEmptyString
    allowed_evidence_ids: tuple[UUID, ...]
    tool_policy_id: NonEmptyString
    model_policy_id: NonEmptyString
    budget_ref: NonEmptyString
    deadline: UTCDateTime


class ResearchClaim(ContractModel):
    text: NonEmptyString
    evidence_refs: tuple[UUID, ...] = ()
    hypothesis: bool = False

    @model_validator(mode="after")
    def require_evidence_or_hypothesis(self) -> Self:
        if not self.evidence_refs and not self.hypothesis:
            raise ValueError("claim without evidence_refs must be marked hypothesis")
        return self


class Citation(ContractModel):
    claim_index: int = Field(ge=0)
    evidence_id: UUID
    locator: str | None = None


class ResearchArtifact(ContractModel):
    artifact_id: UUID
    request_id: UUID
    subject: NonEmptyString
    instrument_id: UUID | None = None
    as_of: UTCDateTime
    horizon: NonEmptyString
    claims: tuple[ResearchClaim, ...]
    citations: tuple[Citation, ...]
    counterarguments: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    confidence: UnitDecimal
    warnings: tuple[str, ...] = ()
    producer: NonEmptyString
    model_version: NonEmptyString
    tool_versions: tuple[str, ...] = ()
    usage: ModelUsage | None = None
    generated_at: UTCDateTime


class AgentOpinion(ContractModel):
    opinion_id: UUID
    instrument_id: UUID
    as_of: UTCDateTime
    horizon: NonEmptyString
    recommendation: NonEmptyString
    thesis: NonEmptyString
    confidence: UnitDecimal
    calibration: ConfidenceCalibration
    evidence_refs: tuple[UUID, ...] = ()
    producer: NonEmptyString
    model_version: NonEmptyString
    warnings: tuple[str, ...] = ()


class AnalysisBundle(ContractModel):
    bundle_id: UUID
    run_id: UUID
    as_of: UTCDateTime
    analyst_artifact_ids: tuple[UUID, ...]
    debate_artifact_refs: tuple[ArtifactRef, ...] = ()
    research_plan_ref: ArtifactRef | None = None
    opinion_ids: tuple[UUID, ...] = ()
    source_refs: tuple[UUID, ...] = ()
    model_usage: tuple[ModelUsage, ...] = ()
    warnings: tuple[str, ...] = ()
    worker_version: NonEmptyString
