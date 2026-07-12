"""Explicit result and error types shared by domain boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType


class ErrorCode(StrEnum):
    """Stable machine-readable error codes safe for boundary mapping."""

    INVALID_INPUT = "invalid_input"
    CONFIGURATION_INVALID = "configuration_invalid"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    CAPABILITY_DENIED = "capability_denied"
    EGRESS_DENIED = "egress_denied"
    DATA_UNAVAILABLE = "data_unavailable"
    RATE_LIMITED = "rate_limited"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    MODEL_OUTPUT_INVALID = "model_output_invalid"
    TOOL_FAILED = "tool_failed"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True, slots=True)
class StructuredError:
    """An immutable, public-safe error crossing a typed port boundary."""

    code: ErrorCode
    message: str
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise ValueError("error message must not be blank")
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


@dataclass(frozen=True, slots=True)
class Success[T]:
    """Explicit successful result."""

    value: T


@dataclass(frozen=True, slots=True)
class Failure:
    """Explicit failed result; never confused with a missing value."""

    error: StructuredError


type Result[T] = Success[T] | Failure
