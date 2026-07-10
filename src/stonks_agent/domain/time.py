"""Time validation used by point-in-time domain boundaries."""

from __future__ import annotations

from datetime import UTC, datetime

from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)


def normalize_utc(value: object) -> Result[datetime]:
    """Require a timezone-aware datetime and normalize it to UTC."""

    if not isinstance(value, datetime) or value.tzinfo is None:
        return _invalid_time()
    try:
        if value.utcoffset() is None:
            return _invalid_time()
        normalized = value.astimezone(UTC)
    except (OverflowError, ValueError):
        return _invalid_time()
    return Success(normalized)


def _invalid_time() -> Failure:
    return Failure(
        StructuredError(
            code=ErrorCode.INVALID_INPUT,
            message="A timezone-aware datetime is required",
            details={"field": "datetime"},
        )
    )
