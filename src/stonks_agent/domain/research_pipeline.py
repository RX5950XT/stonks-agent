"""Canonical research pipeline command and audit result contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks_agent.domain.analysis_context import AnalysisContextRequest
from stonks_contracts.common import ArtifactRef, NonEmptyString, UTCDateTime
from stonks_contracts.report import AnalysisReport


class PipelineStatus(StrEnum):
    SUCCEEDED = "succeeded"
    DEGRADED = "degraded"
    FAILED = "failed"


class PipelineStage(StrEnum):
    BUDGET = "budget"
    CONTEXT = "context"
    DETERMINISTIC = "deterministic"
    TRADINGAGENTS = "tradingagents"
    REPORT = "report"
    RENDER = "render"
    DEADLINE = "deadline"


class PipelineIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: PipelineStage
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")


class ResearchPipelineCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    owner_subject: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/+=-]{0,254}$",
    )
    context_request: AnalysisContextRequest
    report_request_id: UUID
    report_id: UUID
    language: str = Field(pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$")
    report_type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
    policy_version: NonEmptyString
    max_output_tokens: int = Field(ge=1, le=1_000_000)
    deadline_at: UTCDateTime

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if self.context_request.run_id != self.run_id:
            raise ValueError("pipeline context run identity changed")
        if self.deadline_at <= self.context_request.as_of:
            raise ValueError("pipeline deadline must be after as_of")
        return self


class ResearchPipelineResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    status: PipelineStatus
    context_id: UUID
    research_artifact_id: UUID | None = None
    opinion_id: UUID | None = None
    report: AnalysisReport | None = None
    issues: tuple[PipelineIssue, ...] = ()
    audit_artifact_ref: ArtifactRef
