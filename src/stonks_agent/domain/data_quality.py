"""Typed provider outcomes; empty and failure states never collapse."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stonks_contracts.common import UnitDecimal, UTCDateTime


class ProviderDataState(StrEnum):
    AVAILABLE = "available"
    LEGITIMATE_EMPTY = "legitimate_empty"
    NOT_SUPPORTED = "not_supported"
    CONFIG_MISSING = "config_missing"
    HEALTH_UNKNOWN = "health_unknown"
    PROVIDER_UNHEALTHY = "provider_unhealthy"
    FRESHNESS_UNKNOWN = "freshness_unknown"
    QUOTA_UNKNOWN = "quota_unknown"
    QUOTA_EXHAUSTED = "quota_exhausted"
    STALE = "stale"
    PARTIAL = "partial"
    CONFLICT = "conflict"
    FETCH_FAILED = "fetch_failed"


_FAILURE_STATES = frozenset(
    {
        ProviderDataState.NOT_SUPPORTED,
        ProviderDataState.CONFIG_MISSING,
        ProviderDataState.HEALTH_UNKNOWN,
        ProviderDataState.PROVIDER_UNHEALTHY,
        ProviderDataState.FRESHNESS_UNKNOWN,
        ProviderDataState.QUOTA_UNKNOWN,
        ProviderDataState.QUOTA_EXHAUSTED,
        ProviderDataState.CONFLICT,
        ProviderDataState.FETCH_FAILED,
    }
)


class ProviderHealthState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class ProviderRuntimeHealth(BaseModel):
    """Point-in-time provider health used before spending a request budget."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: ProviderHealthState
    checked_at: UTCDateTime
    latest_data_at: UTCDateTime | None = None
    remaining_quota: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_timeline(self) -> Self:
        if self.latest_data_at is not None and self.latest_data_at > self.checked_at:
            raise ValueError("latest provider data cannot postdate its health check")
        return self


class ProviderObservation[T](BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: ProviderDataState
    data: tuple[T, ...]
    completeness: UnitDecimal
    reasons: tuple[str, ...] = ()
    observed_at: UTCDateTime

    @field_validator("reasons")
    @classmethod
    def validate_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not reason.strip() for reason in value):
            raise ValueError("provider observation reasons must not be blank")
        return value

    @model_validator(mode="after")
    def validate_state_payload(self) -> Self:
        if self.state is ProviderDataState.AVAILABLE:
            if not self.data or self.completeness != 1:
                raise ValueError("available state requires complete non-empty data")
        elif self.state is ProviderDataState.LEGITIMATE_EMPTY:
            if self.data or self.completeness != 1:
                raise ValueError("legitimate empty state requires complete empty data")
        elif self.state is ProviderDataState.PARTIAL:
            if not self.data or not 0 < self.completeness < 1 or not self.reasons:
                raise ValueError(
                    "partial state requires incomplete non-empty data and a reason"
                )
        elif self.state is ProviderDataState.STALE:
            if not self.data or self.completeness != 1 or not self.reasons:
                raise ValueError(
                    "stale state requires complete non-empty data and a reason"
                )
        elif self.state in _FAILURE_STATES and (
            self.data or self.completeness != 0 or not self.reasons
        ):
            raise ValueError("failure state requires empty data and an explicit reason")
        return self

    @property
    def is_usable(self) -> bool:
        return self.state in {
            ProviderDataState.AVAILABLE,
            ProviderDataState.LEGITIMATE_EMPTY,
        }

    def accepted(self, *, allow_stale: bool, allow_partial: bool) -> bool:
        return (
            self.is_usable
            or (allow_stale and self.state is ProviderDataState.STALE)
            or (allow_partial and self.state is ProviderDataState.PARTIAL)
        )
