"""Canonical instrument identity contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import model_validator

from .common import ContractModel, Currency, NonEmptyString, UTCDateTime


class AssetClass(StrEnum):
    EQUITY = "equity"
    ETF = "etf"
    FUND = "fund"
    INDEX = "index"
    FX = "fx"
    CRYPTO = "crypto"
    FUTURE = "future"
    OPTION = "option"


class ProviderSymbol(ContractModel):
    provider: NonEmptyString
    symbol: NonEmptyString
    valid_from: UTCDateTime
    valid_to: UTCDateTime | None = None

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        return self


class InstrumentKey(ContractModel):
    instrument_id: UUID
    asset_class: AssetClass
    primary_symbol: NonEmptyString
    exchange_mic: NonEmptyString
    currency: Currency
    timezone: NonEmptyString
    provider_symbols: tuple[ProviderSymbol, ...] = ()
