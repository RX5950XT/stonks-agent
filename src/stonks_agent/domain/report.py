"""Strict structured report draft and generation command contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks_agent.domain.analysis_context import AnalysisContext
from stonks_contracts.common import NonEmptyString, UnitDecimal, UTCDateTime
from stonks_contracts.market_data import DataQualityStatus
from stonks_contracts.report import ClaimCertainty, ReportReference


class ResearchOutlook(StrEnum):
    BULLISH = "bullish_outlook"
    NEUTRAL = "neutral_outlook"
    BEARISH = "bearish_outlook"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class DraftClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    assertion: str = Field(min_length=1, max_length=4_000)
    certainty: ClaimCertainty
    data_quality: DataQualityStatus | None
    evidence_refs: tuple[UUID, ...] = Field(default_factory=tuple, max_length=128)


class ReportDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outlook: ResearchOutlook
    score: UnitDecimal
    confidence: UnitDecimal
    claims: tuple[DraftClaim, ...] = Field(min_length=1, max_length=256)
    risks: tuple[NonEmptyString, ...] = Field(default_factory=tuple, max_length=64)
    catalysts: tuple[NonEmptyString, ...] = Field(default_factory=tuple, max_length=64)
    scenarios: tuple[NonEmptyString, ...] = Field(default_factory=tuple, max_length=64)
    signal_attribution: tuple[NonEmptyString, ...] = Field(
        default_factory=tuple, max_length=64
    )
    data_limitations: tuple[NonEmptyString, ...] = Field(
        default_factory=tuple, max_length=128
    )


class GenerateReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    report_id: UUID
    context: AnalysisContext
    language: str = Field(min_length=2, max_length=32)
    report_type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
    policy_version: NonEmptyString
    signal_ids: tuple[UUID, ...] = Field(default_factory=tuple, max_length=256)
    portfolio_target_refs: tuple[ReportReference, ...] = Field(
        default_factory=tuple, max_length=100_000
    )
    risk_decision_refs: tuple[ReportReference, ...] = Field(
        default_factory=tuple, max_length=100_000
    )
    order_intent_refs: tuple[ReportReference, ...] = Field(
        default_factory=tuple, max_length=100_000
    )
    fill_refs: tuple[ReportReference, ...] = Field(
        default_factory=tuple, max_length=100_000
    )
    outcome_refs: tuple[ReportReference, ...] = Field(
        default_factory=tuple, max_length=100_000
    )
    max_output_tokens: int = Field(ge=1, le=1_000_000)
    deadline_at: UTCDateTime

    @model_validator(mode="after")
    def validate_deadline(self) -> Self:
        if self.deadline_at <= self.context.as_of:
            raise ValueError("report deadline must be later than context as_of")
        if len(self.signal_ids) != len(set(self.signal_ids)):
            raise ValueError("report signal IDs must be unique")
        for references in (
            self.portfolio_target_refs,
            self.risk_decision_refs,
            self.order_intent_refs,
            self.fill_refs,
            self.outcome_refs,
        ):
            identifiers = tuple(item.ref_id for item in references)
            if len(identifiers) != len(set(identifiers)):
                raise ValueError("report trading reference IDs must be unique")
        return self
