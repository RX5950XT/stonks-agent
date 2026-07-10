"""Validated canonical market-data values."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from stonks_agent.domain.evidence import EvidenceTimeline
from stonks_contracts.common import DecimalString, NonNegativeDecimal


class OHLCBar(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    timeline: EvidenceTimeline
    open: DecimalString
    high: DecimalString
    low: DecimalString
    close: DecimalString
    volume: NonNegativeDecimal
    amount: NonNegativeDecimal | None = None

    @model_validator(mode="after")
    def validate_ohlc(self) -> Self:
        if self.high < self.low:
            raise ValueError("OHLC high must be greater than or equal to low")
        if not self.low <= self.open <= self.high:
            raise ValueError("OHLC open must be within low/high")
        if not self.low <= self.close <= self.high:
            raise ValueError("OHLC close must be within low/high")
        return self
