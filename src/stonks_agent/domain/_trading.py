"""Shared immutable trading-domain validation helpers."""

from __future__ import annotations

from decimal import Decimal, DecimalException

from pydantic import BaseModel, ConfigDict

from stonks_agent.domain.errors import ErrorCode, Failure, StructuredError


class TradingModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
        str_strip_whitespace=False,
    )


def is_quantized(value: Decimal, quantum: Decimal) -> bool:
    if not value.is_finite() or not quantum.is_finite() or quantum <= 0:
        return False
    try:
        return value % quantum == 0
    except DecimalException:
        return False


def failure(
    code: ErrorCode,
    message: str,
    **details: object,
) -> Failure:
    return Failure(StructuredError(code=code, message=message, details=details))
