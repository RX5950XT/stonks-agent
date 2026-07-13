"""Forecast artifacts, promoted alpha signals, and fail-closed eligibility."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from stonks_agent.domain.evaluation import EvaluationReport
from stonks_agent.domain.strategy import PromotionState, StrategyRegistryEntry
from stonks_contracts.common import (
    ArtifactRef,
    ConfidenceCalibration,
    Sha256,
    SignedUnitDecimal,
    UnitDecimal,
    UTCDateTime,
)
from stonks_contracts.signal import ForecastSignal


class SignalDirection(StrEnum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


class SignalSource(StrEnum):
    DETERMINISTIC = "deterministic"
    FORECAST = "forecast"
    OPINION = "opinion"


class ForecastRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    run_id: UUID
    instrument_id: UUID
    dataset_snapshot_id: UUID
    snapshot_artifact_ref: ArtifactRef
    data_hash: Sha256
    as_of: UTCDateTime
    interval: str = Field(pattern=r"^[1-9][0-9]*[mhdw]$")
    horizon_bars: int = Field(ge=1, le=10_000)
    input_window_start: UTCDateTime
    input_window_end: UTCDateTime
    model_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
    model_revision: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
    model_artifact_hash: Sha256
    tokenizer_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
    tokenizer_revision: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
    tokenizer_artifact_hash: Sha256
    runtime_hash: Sha256
    requested_at: UTCDateTime
    deadline_at: UTCDateTime

    @model_validator(mode="after")
    def validate_timeline(self) -> Self:
        if self.input_window_end <= self.input_window_start:
            raise ValueError("input window end must be later than start")
        if self.input_window_end > self.as_of:
            raise ValueError("input window cannot extend beyond as_of")
        if self.requested_at < self.as_of:
            raise ValueError("forecast request cannot precede as_of")
        if self.deadline_at <= self.requested_at:
            raise ValueError("forecast deadline must be later than request time")
        return self


class ForecastOutputArtifact(BaseModel):
    """Archived worker result; stochastic replay starts from these artifacts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    forecast: ForecastSignal
    raw_output_artifact_ref: ArtifactRef
    sampled_paths_artifact_ref: ArtifactRef | None = None
    model_artifact_hash: Sha256
    tokenizer_artifact_hash: Sha256
    runtime_hash: Sha256
    data_hash: Sha256
    stochastic: bool
    created_at: UTCDateTime

    @model_validator(mode="after")
    def validate_artifact(self, info: ValidationInfo) -> Self:
        if self.stochastic and self.sampled_paths_artifact_ref is None:
            raise ValueError("stochastic forecast must archive sampled paths")
        if self.created_at < self.forecast.generated_at:
            raise ValueError("forecast artifact cannot precede generated output")
        request = info.context.get("request") if info.context else None
        if isinstance(request, ForecastRequest) and not _matches_request(self, request):
            raise ValueError("forecast output does not match request")
        return self


class AlphaSignal(BaseModel):
    """Research signal with exact provenance; it has no target/order authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    signal_id: UUID
    strategy_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    strategy_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    instrument_id: UUID
    as_of: UTCDateTime
    generated_at: UTCDateTime
    stale_at: UTCDateTime
    expires_at: UTCDateTime
    horizon: str = Field(min_length=1, max_length=128)
    value: SignedUnitDecimal
    confidence: UnitDecimal
    calibration: ConfidenceCalibration
    direction: SignalDirection
    source: SignalSource
    strategy_manifest_hash: Sha256
    dataset_snapshot_id: UUID
    data_hash: Sha256
    runtime_hash: Sha256
    evaluation_policy_hash: Sha256
    raw_output_artifact_ref: ArtifactRef
    evaluation_report_id: UUID | None = None
    evaluation_hash: Sha256 | None = None
    evidence_refs: tuple[UUID, ...] = Field(default_factory=tuple, max_length=256)
    forecast_refs: tuple[UUID, ...] = Field(default_factory=tuple, max_length=256)
    reason_codes: tuple[str, ...] = Field(default_factory=tuple, max_length=64)

    @field_validator("evidence_refs", "forecast_refs", "reason_codes")
    @classmethod
    def validate_unique_values(cls, values: tuple[object, ...]) -> tuple[object, ...]:
        if len(values) != len(set(values)):
            raise ValueError("signal references and reason codes must be unique")
        if any(isinstance(value, str) and not value.strip() for value in values):
            raise ValueError("signal reason codes must not be blank")
        return values

    @model_validator(mode="after")
    def validate_integrity(self) -> Self:
        if not self.as_of <= self.generated_at < self.stale_at < self.expires_at:
            raise ValueError("signal freshness timeline is invalid")
        direction_matches = {
            SignalDirection.LONG: self.value > 0,
            SignalDirection.SHORT: self.value < 0,
            SignalDirection.NEUTRAL: self.value == 0,
        }
        if not direction_matches[self.direction]:
            raise ValueError("signal direction does not match signed value")
        has_report = self.evaluation_report_id is not None
        if has_report != (self.evaluation_hash is not None):
            raise ValueError("signal evaluation id and hash must be bound together")
        if self.source is SignalSource.FORECAST and not self.forecast_refs:
            raise ValueError("forecast-derived alpha requires a forecast reference")
        return self


class SignalEligibilityDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    eligible: bool
    weight: UnitDecimal
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_weight(self) -> Self:
        if not self.eligible and self.weight != 0:
            raise ValueError("ineligible signals must have zero weight")
        return self


def evaluate_signal_eligibility(
    signal: AlphaSignal,
    *,
    registry: StrategyRegistryEntry | None,
    evaluation: EvaluationReport | None,
    at: UTCDateTime,
) -> SignalEligibilityDecision:
    reason = _ineligibility_reason(signal, registry, evaluation, at)
    if reason is not None:
        return SignalEligibilityDecision(
            eligible=False,
            weight=Decimal(0),
            reason_codes=(reason,),
        )
    return SignalEligibilityDecision(
        eligible=True,
        weight=signal.confidence,
        reason_codes=("eligible",),
    )


def _ineligibility_reason(
    signal: AlphaSignal,
    registry: StrategyRegistryEntry | None,
    evaluation: EvaluationReport | None,
    at: UTCDateTime,
) -> str | None:
    if at >= signal.expires_at:
        return "signal_expired"
    if at >= signal.stale_at:
        return "signal_stale"
    if signal.calibration is not ConfidenceCalibration.CALIBRATED:
        return "uncalibrated"
    if registry is None:
        return "strategy_unregistered"
    if registry.state is not PromotionState.PAPER_ELIGIBLE:
        return "strategy_not_paper_eligible"
    if not _strategy_binding_matches(signal, registry):
        return "strategy_binding_mismatch"
    if evaluation is None or not evaluation.passed:
        return "evaluation_not_passed"
    if evaluation.valid_until <= at:
        return "evaluation_expired"
    if not _evaluation_binding_matches(signal, registry, evaluation):
        return "evaluation_binding_mismatch"
    return None


def _strategy_binding_matches(
    signal: AlphaSignal,
    registry: StrategyRegistryEntry,
) -> bool:
    manifest = registry.manifest
    return (
        signal.strategy_id == manifest.strategy_id
        and signal.strategy_version == manifest.strategy_version
        and signal.strategy_manifest_hash == manifest.manifest_hash
        and signal.runtime_hash == manifest.runtime_hash
    )


def _evaluation_binding_matches(
    signal: AlphaSignal,
    registry: StrategyRegistryEntry,
    evaluation: EvaluationReport,
) -> bool:
    return (
        signal.evaluation_report_id == evaluation.report_id
        and signal.evaluation_hash == evaluation.evaluation_hash
        and registry.evaluation_report_id == evaluation.report_id
        and registry.evaluation_hash == evaluation.evaluation_hash
        and signal.strategy_id == evaluation.strategy_id
        and signal.strategy_version == evaluation.strategy_version
        and signal.strategy_manifest_hash == evaluation.strategy_manifest_hash
        and signal.runtime_hash == evaluation.runtime_hash
        and signal.evaluation_policy_hash == evaluation.evaluation_policy_hash
    )


def _matches_request(output: ForecastOutputArtifact, request: ForecastRequest) -> bool:
    forecast = output.forecast
    return (
        output.request_id == request.request_id
        and forecast.instrument_id == request.instrument_id
        and forecast.dataset_snapshot_id == request.dataset_snapshot_id
        and forecast.as_of == request.as_of
        and forecast.interval == request.interval
        and forecast.horizon_bars == request.horizon_bars
        and forecast.input_window_start == request.input_window_start
        and forecast.input_window_end == request.input_window_end
        and forecast.model_id == request.model_id
        and forecast.model_revision == request.model_revision
        and forecast.tokenizer_id == request.tokenizer_id
        and forecast.tokenizer_revision == request.tokenizer_revision
        and output.model_artifact_hash == request.model_artifact_hash
        and output.tokenizer_artifact_hash == request.tokenizer_artifact_hash
        and output.runtime_hash == request.runtime_hash
        and output.data_hash == request.data_hash
        and output.created_at <= request.deadline_at
    )
