"""Forecast and promoted alpha signal contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from .common import (
    ContractModel,
    DecimalString,
    NonEmptyString,
    NonNegativeDecimal,
    SignedUnitDecimal,
    UnitDecimal,
    UTCDateTime,
)
from .market_data import DataQuality


class SignalDirection(StrEnum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


class PromotionState(StrEnum):
    DRAFT = "draft"
    EVALUATING = "evaluating"
    REJECTED = "rejected"
    SHADOW = "shadow"
    PAPER_ELIGIBLE = "paper_eligible"
    SUSPENDED = "suspended"
    RETIRED = "retired"


class ForecastSignal(ContractModel):
    forecast_id: UUID
    instrument_id: UUID
    as_of: UTCDateTime
    interval: NonEmptyString
    horizon_bars: int = Field(gt=0)
    expected_return: DecimalString
    median_return: DecimalString
    direction_probability: UnitDecimal
    expected_volatility: NonNegativeDecimal
    downside_quantile: DecimalString
    max_drawdown_quantile: DecimalString
    path_count: int = Field(gt=0)
    dispersion: NonNegativeDecimal
    calibration_bucket: str | None = None
    input_quality: DataQuality
    model_id: NonEmptyString
    model_revision: NonEmptyString
    tokenizer_id: NonEmptyString
    tokenizer_revision: NonEmptyString
    device: NonEmptyString
    seed_policy: NonEmptyString
    inference_code_version: NonEmptyString
    dataset_snapshot_id: UUID
    input_window_start: UTCDateTime
    input_window_end: UTCDateTime
    generated_at: UTCDateTime
    latency_ms: int = Field(ge=0)
    validity_warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.input_window_end <= self.input_window_start:
            raise ValueError("input_window_end must be later than input_window_start")
        if self.input_window_end > self.as_of:
            raise ValueError("input_window_end cannot be later than as_of")
        return self


class AlphaSignal(ContractModel):
    signal_id: UUID
    strategy_id: NonEmptyString
    strategy_version: NonEmptyString
    instrument_id: UUID
    as_of: UTCDateTime
    horizon: NonEmptyString
    value: SignedUnitDecimal
    confidence: UnitDecimal
    expires_at: UTCDateTime
    direction: SignalDirection
    evidence_refs: tuple[UUID, ...] = ()
    forecast_refs: tuple[UUID, ...] = ()
    evaluation_report_id: UUID | None = None
    reason_codes: tuple[str, ...] = ()
    promotion_state: PromotionState

    @model_validator(mode="after")
    def validate_expiry(self) -> Self:
        if self.expires_at <= self.as_of:
            raise ValueError("expires_at must be later than as_of")
        return self
