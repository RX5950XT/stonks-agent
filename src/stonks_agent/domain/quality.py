"""Explicit quality states; infrastructure failures are never empty success."""

from __future__ import annotations

from collections.abc import Set
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator


class QualityState(StrEnum):
    VALID = "valid"
    STALE = "stale"
    PARTIAL = "partial"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class QualityAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: QualityState
    reasons: tuple[str, ...] = ()

    @field_validator("reasons")
    @classmethod
    def validate_reasons(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("quality reasons must not be blank")
        return values

    def is_acceptable(self, allowed_states: Set[QualityState]) -> bool:
        return self.state in allowed_states
