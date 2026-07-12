"""Point-in-time canonical instrument identity."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from itertools import pairwise
from typing import Self
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stonks_contracts.common import Currency, NonEmptyString, UTCDateTime
from stonks_contracts.instrument import AssetClass


class ProviderSymbolMapping(BaseModel):
    """A provider symbol valid over a half-open UTC interval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    symbol: NonEmptyString
    valid_from: UTCDateTime
    valid_to: UTCDateTime | None = None

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        return self

    def is_active_at(self, as_of: datetime) -> bool:
        _require_aware(as_of)
        return self.valid_from <= as_of and (
            self.valid_to is None or as_of < self.valid_to
        )


class Instrument(BaseModel):
    """Provider-independent identity with validated historical aliases."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_id: UUID
    asset_class: AssetClass
    primary_symbol: NonEmptyString
    exchange_mic: str = Field(pattern=r"^[A-Z0-9]{4}$")
    currency: Currency
    timezone: str = Field(min_length=1, max_length=64)
    provider_symbols: tuple[ProviderSymbolMapping, ...] = ()

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timezone must be an IANA timezone") from error
        return value

    @model_validator(mode="after")
    def reject_overlapping_symbol_windows(self) -> Self:
        grouped: dict[str, list[ProviderSymbolMapping]] = defaultdict(list)
        for item in self.provider_symbols:
            grouped[item.provider].append(item)
        for provider, items in grouped.items():
            ordered = sorted(items, key=lambda item: item.valid_from)
            for previous, current in pairwise(ordered):
                if previous.valid_to is None or current.valid_from < previous.valid_to:
                    raise ValueError(f"provider symbol windows overlap for {provider}")
        return self

    def provider_symbol(self, provider: str, as_of: datetime) -> str:
        _require_aware(as_of)
        normalized = provider.strip().lower()
        matches = tuple(
            item.symbol
            for item in self.provider_symbols
            if item.provider == normalized and item.is_active_at(as_of)
        )
        if len(matches) != 1:
            raise LookupError(
                f"no provider symbol for {normalized or '<blank>'} at requested as_of"
            )
        return matches[0]


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
