"""Bounded company and filing projections for the instrument dashboard."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from stonks_contracts.common import UTCDateTime


def _safe_text(value: str) -> str:
    if value.strip() != value or any(ord(character) < 32 for character in value):
        raise ValueError("instrument data text is unsafe")
    return value


class InstrumentDataQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^[A-Z0-9][A-Z0-9.-]*$",
    )
    as_of: UTCDateTime

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value


class InstrumentObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str = Field(min_length=1, max_length=256)
    unit: str = Field(min_length=1, max_length=64)
    period: str | None = Field(default=None, max_length=64)
    event_time: UTCDateTime
    published_at: UTCDateTime | None = None
    available_at: UTCDateTime

    @field_validator("value", "unit", "period")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        return _safe_text(value) if value is not None else value


class InstrumentFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    label: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=256)
    unit: str = Field(min_length=1, max_length=64)
    period: str | None = Field(default=None, max_length=64)
    event_time: UTCDateTime
    published_at: UTCDateTime | None = None
    available_at: UTCDateTime
    provider: str = Field(min_length=1, max_length=64)
    source_url: str = Field(min_length=1, max_length=512)
    history: tuple[InstrumentObservation, ...] = Field(default=(), max_length=12)

    @field_validator(
        "label",
        "value",
        "unit",
        "period",
        "provider",
        "source_url",
    )
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        return _safe_text(value) if value is not None else value


class InstrumentFiling(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    form: str = Field(min_length=1, max_length=32)
    filed_at: UTCDateTime
    period_end: UTCDateTime | None = None
    description: str | None = Field(default=None, max_length=256)
    provider: str = Field(min_length=1, max_length=64)
    source_url: str = Field(min_length=1, max_length=512)

    @field_validator("form", "description", "provider", "source_url")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        return _safe_text(value) if value is not None else value


class InstrumentOverview(BaseModel):
    """Truthful projection; partial means one official endpoint was unavailable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^[A-Z0-9][A-Z0-9.-]*$",
    )
    market: str = Field(min_length=2, max_length=12, pattern=r"^[A-Z0-9]+$")
    name: str = Field(min_length=1, max_length=256)
    exchange: str | None = Field(default=None, max_length=64)
    industry: str | None = Field(default=None, max_length=256)
    cik: str | None = Field(default=None, max_length=16)
    state: Literal["available", "partial"]
    provider: str = Field(min_length=1, max_length=64)
    observed_at: UTCDateTime
    as_of: UTCDateTime
    facts: tuple[InstrumentFact, ...] = Field(default=(), max_length=64)
    filings: tuple[InstrumentFiling, ...] = Field(default=(), max_length=32)
    warnings: tuple[str, ...] = Field(default=(), max_length=16)

    @field_validator("name", "exchange", "industry", "cik")
    @classmethod
    def validate_identity_text(cls, value: str | None) -> str | None:
        return _safe_text(value) if value is not None else value

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("warnings")
    @classmethod
    def validate_warnings(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _safe_text(value)
        return values

    @property
    def has_data(self) -> bool:
        return bool(self.facts or self.filings)
