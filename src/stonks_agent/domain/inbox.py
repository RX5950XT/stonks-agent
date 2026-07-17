"""Validated, immutable inbox message and receipt contracts."""

from __future__ import annotations

import json
import math
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks_agent.domain.telemetry import TraceCarrier
from stonks_contracts.common import Sha256, UTCDateTime, stable_payload_hash

MAX_INBOX_JSON_BYTES = 64 * 1024
MAX_INBOX_JSON_DEPTH = 8


class InboxMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    consumer: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]*$",
    )
    message_id: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]*$",
    )
    payload: dict[str, object]
    received_at: UTCDateTime
    processed_at: UTCDateTime
    trace_carrier: TraceCarrier | None = None
    correlation_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )

    @model_validator(mode="after")
    def validate_message(self) -> Self:
        _validate_json_object(self.payload)
        if self.processed_at < self.received_at:
            raise ValueError("processed_at cannot precede received_at")
        return self

    @property
    def payload_hash(self) -> str:
        return stable_payload_hash(self.payload)


class InboxReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    consumer: str
    message_id: str
    payload_hash: Sha256
    duplicate: bool
    processed_at: UTCDateTime
    result: dict[str, object]
    trace_carrier: TraceCarrier | None = None
    correlation_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        _validate_json_object(self.result)
        return self


def _validate_json_object(value: dict[str, object]) -> None:
    _validate_json(value, depth=0)
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > MAX_INBOX_JSON_BYTES:
        raise ValueError("inbox JSON exceeds size limit")


def _validate_json(value: object, *, depth: int) -> None:
    if depth > MAX_INBOX_JSON_DEPTH:
        raise ValueError("inbox JSON nesting is too deep")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("inbox JSON numbers must be finite")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 128:
                raise ValueError("inbox JSON keys are invalid")
            _validate_json(item, depth=depth + 1)
        return
    raise ValueError("inbox payload contains a non-JSON value")
