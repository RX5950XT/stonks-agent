"""Fenced research lease input and immutable worker result."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks_agent.domain.errors import ErrorCode
from stonks_agent.domain.research import ResearchArtifact
from stonks_agent.domain.research_pipeline import PipelineStatus, ResearchPipelineResult
from stonks_agent.domain.research_run import ResearchRunRequest
from stonks_agent.domain.signal import (
    AlphaSignal,
    ForecastOutputArtifact,
    SignalEligibilityDecision,
)
from stonks_agent.domain.strategy import PromotionState
from stonks_contracts.common import Sha256
from stonks_contracts.evidence import EvidenceItem


class SnapshotForecastContext(BaseModel):
    """Exact immutable snapshot identity required by a forecast worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: UUID
    manifest_artifact_hash: Sha256
    content_hash: Sha256
    provider: str = Field(min_length=1, max_length=128)
    endpoint: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_manifest_identity(self) -> Self:
        if self.manifest_artifact_hash != self.content_hash:
            raise ValueError("snapshot manifest and content hashes must match")
        return self

    @property
    def artifact_ref(self) -> str:
        return f"sha256:{self.manifest_artifact_hash}"


class ResearchLeaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request: ResearchRunRequest
    snapshot: SnapshotForecastContext
    evidence: tuple[EvidenceItem, ...] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        identifiers = tuple(item.evidence_id for item in self.evidence)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("research lease evidence must be unique")
        if any(
            item.subject != self.request.instrument_id
            or item.available_at > self.request.as_of
            or item.as_of > self.request.as_of
            for item in self.evidence
        ):
            raise ValueError("research lease evidence exceeds request scope")
        if self.snapshot.snapshot_id != self.request.snapshot_id:
            raise ValueError("research lease snapshot identity changed")
        return self


class KronosResearchOutcome(BaseModel):
    """Snapshot-bound forecast plus an explicit non-trading authority decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    snapshot_id: UUID
    status: Literal["succeeded", "failed"]
    actual_model_inference: bool
    forecast_output: ForecastOutputArtifact | None = None
    alpha_status: Literal["blocked", "mapped"]
    alpha_signal: AlphaSignal | None = None
    eligibility: SignalEligibilityDecision
    deployment_state: PromotionState = PromotionState.SHADOW
    error_code: ErrorCode | None = None

    @classmethod
    def failed(
        cls,
        *,
        run_id: UUID,
        snapshot_id: UUID,
        error_code: ErrorCode,
    ) -> Self:
        return cls(
            run_id=run_id,
            snapshot_id=snapshot_id,
            status="failed",
            actual_model_inference=False,
            alpha_status="blocked",
            eligibility=SignalEligibilityDecision(
                eligible=False,
                weight=Decimal(0),
                reason_codes=("forecast_unavailable", error_code.value),
            ),
            error_code=error_code,
        )

    @classmethod
    def forecast_succeeded(
        cls,
        *,
        run_id: UUID,
        snapshot_id: UUID,
        forecast_output: ForecastOutputArtifact,
    ) -> Self:
        return cls(
            run_id=run_id,
            snapshot_id=snapshot_id,
            status="succeeded",
            actual_model_inference=True,
            forecast_output=forecast_output,
            alpha_status="blocked",
            eligibility=SignalEligibilityDecision(
                eligible=False,
                weight=Decimal(0),
                reason_codes=(
                    "strategy_authority_unavailable",
                    "strategy_not_paper_eligible",
                ),
            ),
        )

    @model_validator(mode="after")
    def validate_authority(self) -> Self:
        forecast = self.forecast_output
        failed_shape = (
            self.status == "failed"
            and not self.actual_model_inference
            and forecast is None
            and self.error_code is not None
        )
        succeeded_shape = (
            self.status == "succeeded"
            and self.actual_model_inference
            and forecast is not None
            and self.error_code is None
            and forecast.forecast.dataset_snapshot_id == self.snapshot_id
        )
        if not (failed_shape or succeeded_shape):
            raise ValueError("Kronos research outcome shape is inconsistent")
        if self.alpha_status == "blocked":
            if (
                self.alpha_signal is not None
                or self.eligibility.eligible
                or self.eligibility.weight != 0
            ):
                raise ValueError("blocked Kronos alpha must have zero authority")
            return self
        alpha = self.alpha_signal
        if (
            alpha is None
            or forecast is None
            or alpha.dataset_snapshot_id != self.snapshot_id
            or forecast.forecast.forecast_id not in alpha.forecast_refs
        ):
            raise ValueError("mapped Kronos alpha is not forecast-bound")
        return self


class ResearchWorkerResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0.0", "1.1.0"] = "1.0.0"
    research_artifact: ResearchArtifact
    pipeline: ResearchPipelineResult
    kronos: KronosResearchOutcome | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if (
            self.research_artifact.run_id != self.pipeline.run_id
            or self.pipeline.research_artifact_id != self.research_artifact.artifact_id
        ):
            raise ValueError("research worker result identity changed")
        if (self.schema_version == "1.0.0") != (self.kronos is None):
            raise ValueError("research worker result schema does not match Kronos data")
        if self.kronos is not None and (
            self.kronos.run_id != self.research_artifact.run_id
            or (
                self.kronos.forecast_output is not None
                and self.kronos.forecast_output.forecast.as_of
                > self.research_artifact.as_of
            )
        ):
            raise ValueError("research worker Kronos identity changed")
        return self

    @property
    def status(self) -> PipelineStatus:
        if self.pipeline.status is PipelineStatus.FAILED:
            return PipelineStatus.FAILED
        if self.pipeline.status is PipelineStatus.DEGRADED or (
            self.kronos is not None and self.kronos.status == "failed"
        ):
            return PipelineStatus.DEGRADED
        return self.pipeline.status
