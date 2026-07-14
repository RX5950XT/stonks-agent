"""Shared primitives for versioned Stonks Agent wire contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
)

SchemaVersion = Literal["1.0.0"]
SCHEMA_VERSION: SchemaVersion = "1.0.0"
type JsonPrimitive = str | int | bool | None
type JsonValue = object


def _parse_decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, Decimal)):
        raise ValueError("Decimal input must be a string or Decimal")
    if isinstance(value, str) and (not value or value.strip() != value):
        raise ValueError("Decimal string must be non-empty and contain no surrounding whitespace")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("Decimal input is invalid") from error
    if not parsed.is_finite():
        raise ValueError("Decimal input must be finite")
    parts = parsed.as_tuple()
    exponent = parts.exponent
    if (
        not isinstance(exponent, int)
        or len(parts.digits) > 64
        or abs(exponent) > 64
        or abs(parsed.adjusted()) > 64
    ):
        raise ValueError("Decimal input exceeds supported bounds")
    return parsed


def _serialize_decimal(value: Decimal) -> str:
    return format(value, "f")


DECIMAL_SCHEMA = {
    "type": "string",
    "pattern": r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$",
}
DecimalString = Annotated[
    Decimal,
    BeforeValidator(_parse_decimal),
    PlainSerializer(_serialize_decimal, return_type=str),
    WithJsonSchema(DECIMAL_SCHEMA, mode="validation"),
    WithJsonSchema(DECIMAL_SCHEMA, mode="serialization"),
]


def _positive(value: Decimal) -> Decimal:
    if value <= 0:
        raise ValueError("value must be greater than zero")
    return value


def _non_negative(value: Decimal) -> Decimal:
    if value < 0:
        raise ValueError("value must be non-negative")
    return value


def _unit_interval(value: Decimal) -> Decimal:
    if not Decimal("0") <= value <= Decimal("1"):
        raise ValueError("value must be between 0 and 1")
    return value


def _signed_unit_interval(value: Decimal) -> Decimal:
    if not Decimal("-1") <= value <= Decimal("1"):
        raise ValueError("value must be between -1 and 1")
    return value


PositiveDecimal = Annotated[DecimalString, AfterValidator(_positive)]
NonNegativeDecimal = Annotated[DecimalString, AfterValidator(_non_negative)]
UnitDecimal = Annotated[DecimalString, AfterValidator(_unit_interval)]
SignedUnitDecimal = Annotated[DecimalString, AfterValidator(_signed_unit_interval)]


def _parse_utc_datetime(value: object) -> object:
    if isinstance(value, (str, datetime)):
        return value
    raise ValueError("datetime input must be an ISO 8601 string or datetime")


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _serialize_utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


UTCDateTime = Annotated[
    datetime,
    BeforeValidator(_parse_utc_datetime),
    AfterValidator(_normalize_utc),
    PlainSerializer(_serialize_utc, return_type=str),
]

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
ArtifactRef = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
NonEmptyString = Annotated[str, Field(min_length=1)]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


class ContractModel(BaseModel):
    """Immutable, closed base model shared by all wire contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
        str_strip_whitespace=False,
    )

    schema_version: SchemaVersion = SCHEMA_VERSION

    def canonical_json(self) -> str:
        """Return stable UTF-8 JSON suitable for hashing and replay."""
        return canonical_json(self)

    def payload_hash(self) -> str:
        """Return the deterministic SHA-256 digest of the full wire payload."""
        return stable_payload_hash(self)


def _json_payload(value: ContractModel | JsonValue) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def canonical_json(value: ContractModel | JsonValue) -> str:
    """Serialize a model or JSON value deterministically."""
    return json.dumps(
        _json_payload(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def stable_payload_hash(value: ContractModel | JsonValue) -> str:
    """Hash a canonical payload without depending on insertion order."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class Money(ContractModel):
    currency: Currency
    amount: DecimalString


class ModelUsage(ContractModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    estimated_cost: Money | None = None
    latency_ms: int = Field(ge=0)


class ConfidenceCalibration(StrEnum):
    UNCALIBRATED = "uncalibrated"
    CALIBRATED = "calibrated"
