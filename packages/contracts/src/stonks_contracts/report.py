"""Evidence-linked analysis report contracts."""

from __future__ import annotations

from uuid import UUID

from .common import ArtifactRef, ContractModel, NonEmptyString, Sha256, UnitDecimal, UTCDateTime


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
    evidence_refs: tuple[UUID, ...]
    signal_ids: tuple[UUID, ...] = ()
    generator_version: NonEmptyString
    model_version: str | None = None
    prompt_version: str | None = None
    policy_version: NonEmptyString
    renderings: tuple[ReportRendering, ...] = ()
