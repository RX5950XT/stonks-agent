"""Read-only artifact capability issuance boundary."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stonks_agent.domain.artifact_capability import SignedArtifactReadCapability
from stonks_agent.domain.errors import Result


@runtime_checkable
class ArtifactReadCapabilityIssuerPort(Protocol):
    def issue_read_url(
        self,
        content_hash: str,
        *,
        expires_at: object,
    ) -> Result[SignedArtifactReadCapability]: ...
