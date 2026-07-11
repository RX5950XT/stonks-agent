"""Validated snapshot ingestion request and reference-only response."""

from __future__ import annotations

import json
import math
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks_contracts.common import NonEmptyString, UTCDateTime, stable_payload_hash

MAX_QUERY_BYTES = 32 * 1024
MAX_QUERY_DEPTH = 5


class CreateSnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    market: str = Field(pattern=r"^[A-Z0-9]{2,12}$")
    capability: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    as_of: UTCDateTime
    query: dict[str, object]
    provider_policy_id: NonEmptyString
    idempotency_key: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]*$",
    )
    requested_at: UTCDateTime

    @model_validator(mode="after")
    def validate_query(self) -> Self:
        _validate_json(self.query, depth=0)
        encoded = json.dumps(
            self.query,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > MAX_QUERY_BYTES:
            raise ValueError("snapshot query exceeds size limit")
        return self

    @property
    def input_hash(self) -> str:
        return stable_payload_hash(
            {
                "market": self.market,
                "capability": self.capability,
                "as_of": self.as_of.isoformat(),
                "query": self.query,
                "provider_policy_id": self.provider_policy_id,
            }
        )


class SnapshotJobRefs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    job_id: UUID
    snapshot_id: UUID
    evidence_refs: tuple[UUID, ...] = ()


def _validate_json(value: object, *, depth: int) -> None:
    if depth > MAX_QUERY_DEPTH:
        raise ValueError("snapshot query nesting is too deep")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("snapshot query numbers must be finite")
        return
    if isinstance(value, list):
        if len(value) > 1000:
            raise ValueError("snapshot query list is too large")
        for item in value:
            _validate_json(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 256:
            raise ValueError("snapshot query object is too large")
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 128:
                raise ValueError("snapshot query keys are invalid")
            _validate_json(item, depth=depth + 1)
        return
    raise ValueError("snapshot query contains a non-JSON value")
