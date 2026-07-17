"""Immutable rate-limit boundary values."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RateLimitDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    remaining: int = Field(ge=0)
    retry_after_seconds: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_retry_semantics(self) -> Self:
        if self.allowed and self.retry_after_seconds != 0:
            raise ValueError("allowed decisions cannot request a retry")
        if not self.allowed and self.retry_after_seconds < 1:
            raise ValueError("denied decisions require a positive retry")
        return self
