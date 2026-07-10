"""Point-in-time market data contracts."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from .common import (
    ArtifactRef,
    ContractModel,
    DecimalString,
    NonEmptyString,
    NonNegativeDecimal,
    Sha256,
    UnitDecimal,
    UTCDateTime,
)


class DataQualityStatus(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"
    NOT_SUPPORTED = "not_supported"
    FALLBACK = "fallback"
    STALE = "stale"
    ESTIMATED = "estimated"
    PARTIAL = "partial"
    FETCH_FAILED = "fetch_failed"
    CONFLICT = "conflict"


class DataQuality(ContractModel):
    status: DataQualityStatus
    completeness: UnitDecimal
    warnings: tuple[str, ...] = ()
    fallback_chain: tuple[str, ...] = ()


class MarketDataQuery(ContractModel):
    query_id: UUID
    instrument_ids: tuple[UUID, ...] = Field(min_length=1)
    capability: NonEmptyString
    interval: NonEmptyString
    start: UTCDateTime
    end: UTCDateTime
    as_of: UTCDateTime
    adjustment: NonEmptyString
    session: NonEmptyString
    provider_policy_id: NonEmptyString
    freshness_seconds: int = Field(ge=0)
    strict_point_in_time: bool = True

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.end <= self.start:
            raise ValueError("end must be later than start")
        if self.end > self.as_of:
            raise ValueError("end cannot be later than as_of")
        return self


class Bar(ContractModel):
    event_time: UTCDateTime
    published_at: UTCDateTime
    available_at: UTCDateTime
    observed_at: UTCDateTime
    open: DecimalString
    high: DecimalString
    low: DecimalString
    close: DecimalString
    volume: NonNegativeDecimal
    amount: NonNegativeDecimal | None = None
    vwap: NonNegativeDecimal | None = None

    @model_validator(mode="after")
    def validate_market_invariants(self) -> Self:
        open_in_range = self.low <= self.open <= self.high
        close_in_range = self.low <= self.close <= self.high
        if self.low > self.high or not open_in_range or not close_in_range:
            raise ValueError("OHLC must satisfy low <= open/close <= high")
        if self.available_at > self.observed_at:
            raise ValueError("available_at cannot be later than observed_at")
        return self


class BarSeries(ContractModel):
    series_id: UUID
    instrument_id: UUID
    interval: NonEmptyString
    adjustment: NonEmptyString
    session: NonEmptyString
    as_of: UTCDateTime
    provider: NonEmptyString
    endpoint: NonEmptyString
    request_id: str | None = None
    raw_artifact_ref: ArtifactRef
    source_payload_hash: Sha256 | None = None
    quality: DataQuality = DataQuality(
        status=DataQualityStatus.AVAILABLE,
        completeness=Decimal("1"),
    )
    bars: tuple[Bar, ...]

    @model_validator(mode="after")
    def validate_series(self) -> Self:
        event_times = tuple(bar.event_time for bar in self.bars)
        if any(current <= previous for previous, current in pairwise(event_times)):
            raise ValueError("bar event_time values must be strictly increasing and unique")
        if any(bar.available_at > self.as_of for bar in self.bars):
            raise ValueError("bar available_at cannot be later than series as_of")
        return self


class DatasetSnapshot(ContractModel):
    snapshot_id: UUID
    instrument_ids: tuple[UUID, ...]
    calendar_version: NonEmptyString
    query_refs: tuple[UUID, ...]
    artifact_hashes: tuple[Sha256, ...]
    provider_versions: tuple[str, ...]
    corporate_action_refs: tuple[UUID, ...] = ()
    cutoff: UTCDateTime
    created_at: UTCDateTime
