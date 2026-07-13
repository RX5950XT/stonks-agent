"""Evidence-linked analysis report contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import model_validator

from .common import ArtifactRef, ContractModel, NonEmptyString, Sha256, UnitDecimal, UTCDateTime
from .market_data import DataQualityStatus


class ClaimCertainty(StrEnum):
    OBSERVED = "observed"
    QUALIFIED = "qualified"
    HYPOTHESIS = "hypothesis"


class ReportClaim(ContractModel):
    claim_id: UUID
    assertion: NonEmptyString
    certainty: ClaimCertainty
    data_quality: DataQualityStatus | None
    evidence_refs: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def validate_citations_and_certainty(self) -> Self:
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("report claim evidence refs must be unique")
        if self.certainty is ClaimCertainty.HYPOTHESIS:
            if self.evidence_refs or self.data_quality is not None:
                raise ValueError("hypothesis cannot masquerade as evidenced fact")
            return self
        if not self.evidence_refs or self.data_quality is None:
            raise ValueError("evidenced report claim requires citations and quality")
        if (
            self.certainty is ClaimCertainty.OBSERVED
            and self.data_quality is not DataQualityStatus.AVAILABLE
        ):
            raise ValueError("observed claim requires available evidence")
        if (
            self.certainty is ClaimCertainty.QUALIFIED
            and self.data_quality is DataQualityStatus.AVAILABLE
        ):
            raise ValueError("available evidence should not be marked qualified")
        return self


class ReportRendering(ContractModel):
    format: NonEmptyString
    template_version: NonEmptyString
    content_hash: Sha256
    content_ref: ArtifactRef


class AnalysisReport(ContractModel):
    report_id: UUID
    subject: NonEmptyString
    as_of: UTCDateTime
    language: NonEmptyString
    report_type: NonEmptyString
    conclusion: NonEmptyString
    score: UnitDecimal
    confidence: UnitDecimal
    risks: tuple[str, ...] = ()
    catalysts: tuple[str, ...] = ()
    scenarios: tuple[str, ...] = ()
    signal_attribution: tuple[str, ...] = ()
    action_guardrails: tuple[str, ...]
    data_limitations: tuple[str, ...] = ()
    claims: tuple[ReportClaim, ...] = ()
    evidence_refs: tuple[UUID, ...]
    signal_ids: tuple[UUID, ...] = ()
    generator_version: NonEmptyString
    model_version: str | None = None
    prompt_version: str | None = None
    generation_artifact_ref: ArtifactRef | None = None
    policy_version: NonEmptyString
    renderings: tuple[ReportRendering, ...] = ()
