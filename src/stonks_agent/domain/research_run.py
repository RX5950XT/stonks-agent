"""Public research run, canonical event, and report query contracts."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from stonks_contracts.common import (
    NonEmptyString,
    Sha256,
    UTCDateTime,
    stable_payload_hash,
)


class ResearchRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9.-]{0,15}$")
    as_of: UTCDateTime
    snapshot_id: UUID
    research_profile_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
    model_policy_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
    language: str = Field(pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$")
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,127}$")
    owner_subject: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/+=-]{0,254}$",
    )
    requested_at: UTCDateTime
    execution_mode: Literal["paper"] = "paper"

    @property
    def input_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude={"requested_at"})
        return stable_payload_hash(payload)


class ResearchRunRefs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    job_id: UUID


class CanonicalRunEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID
    run_id: UUID
    sequence: int = Field(ge=1)
    event_type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    payload: dict[str, object]
    occurred_at: UTCDateTime
    event_hash: Sha256


class ReportProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    report_id: UUID
    content_hash: Sha256
    format: NonEmptyString
    media_type: Literal["text/markdown", "text/html"]
    content: str = Field(min_length=1, max_length=131_072)
