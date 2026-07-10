"""Immutable evidence and provenance contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import model_validator

from .common import ArtifactRef, ContractModel, JsonValue, NonEmptyString, Sha256, UTCDateTime
from .market_data import DataQuality


class EvidenceKind(StrEnum):
    MARKET_DATA = "market_data"
    FILING = "filing"
    NEWS = "news"
    FUNDAMENTAL = "fundamental"
    COMMUNITY = "community"
    DERIVED = "derived"


class Sensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


class EvidenceItem(ContractModel):
    evidence_id: UUID
    subject: NonEmptyString
    kind: EvidenceKind
    payload: dict[str, JsonValue]
    event_time: UTCDateTime
    published_at: UTCDateTime | None
    available_at: UTCDateTime
    observed_at: UTCDateTime
    as_of: UTCDateTime
    source: NonEmptyString
    provider: NonEmptyString
    source_url: str | None = None
    content_hash: Sha256
    raw_artifact_ref: ArtifactRef
    quality: DataQuality
    sensitivity: Sensitivity = Sensitivity.PUBLIC
    license_tag: NonEmptyString
    redistribution_tag: NonEmptyString
    expires_at: UTCDateTime | None = None
    derived_from: tuple[UUID, ...] = ()
    transformation_version: str | None = None
    untrusted_content: bool = False

    @model_validator(mode="after")
    def validate_point_in_time(self) -> Self:
        if self.available_at > self.observed_at:
            raise ValueError("available_at cannot be later than observed_at")
        if self.available_at > self.as_of:
            raise ValueError("available_at cannot be later than as_of")
        if self.derived_from and not self.transformation_version:
            raise ValueError("derived evidence requires transformation_version")
        return self


class EvidencePack(ContractModel):
    pack_id: UUID
    run_id: UUID
    as_of: UTCDateTime
    evidence_ids: tuple[UUID, ...]
    required_capabilities: tuple[str, ...]
    missing_capabilities: tuple[str, ...] = ()
    quality_summary: tuple[DataQuality, ...]
