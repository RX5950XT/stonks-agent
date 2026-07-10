"""Content-addressed artifact storage contract."""

from __future__ import annotations

from typing import Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks_agent.domain.artifact import ArtifactMetadata
from stonks_agent.domain.errors import Result
from stonks_contracts.common import Sha256, UTCDateTime


class ArtifactManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content_hash: Sha256
    size_bytes: int = Field(ge=0)
    metadata: ArtifactMetadata
    finalized_at: UTCDateTime
    storage_uri: str = Field(pattern=r"^artifact://sha256/[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_uri(self) -> Self:
        if self.storage_uri != f"artifact://sha256/{self.content_hash}":
            raise ValueError("storage_uri must reference content_hash")
        return self


@runtime_checkable
class ArtifactStore(Protocol):
    def finalize(
        self,
        content: object,
        *,
        metadata: object,
        finalized_at: object,
    ) -> Result[ArtifactManifest]: ...

    def read(self, content_hash: str) -> Result[bytes]: ...

    def manifest(self, content_hash: str) -> Result[ArtifactManifest]: ...

    def is_finalized(self, content_hash: str) -> bool: ...
