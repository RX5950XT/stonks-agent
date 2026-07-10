"""Immutable metadata attached to content-addressed artifacts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from stonks_contracts.common import NonEmptyString
from stonks_contracts.evidence import Sensitivity


class ArtifactMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    media_type: str = Field(
        min_length=3,
        max_length=127,
        pattern=r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$",
    )
    license_tag: NonEmptyString
    sensitivity: Sensitivity
    source: NonEmptyString
    attributes: tuple[tuple[str, str], ...] = ()
