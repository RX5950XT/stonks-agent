"""Point-in-time evidence timeline invariants."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from stonks_contracts.common import UTCDateTime


class AvailabilityCertainty(StrEnum):
    PROVEN = "proven"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class EvidenceTimeline(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_time: UTCDateTime
    published_at: UTCDateTime | None
    available_at: UTCDateTime
    observed_at: UTCDateTime
    as_of: UTCDateTime
    availability_certainty: AvailabilityCertainty
    strict_point_in_time: bool = True

    @model_validator(mode="after")
    def validate_timeline(self) -> Self:
        if self.published_at is not None and self.available_at < self.published_at:
            raise ValueError("available_at cannot precede published_at")
        if self.strict_point_in_time and self.available_at > self.as_of:
            raise ValueError("future evidence is forbidden in strict point-in-time mode")
        if self.available_at > self.observed_at:
            raise ValueError("available_at cannot be later than observed_at")
        if (
            self.strict_point_in_time
            and self.availability_certainty is not AvailabilityCertainty.PROVEN
        ):
            raise ValueError("strict point-in-time evidence requires proven availability")
        return self
