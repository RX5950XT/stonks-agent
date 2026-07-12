"""Shared validation for artifact-store adapters."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

from pydantic import ValidationError

from stonks_agent.domain.artifact import ArtifactMetadata
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.ports.artifact_store import ArtifactManifest

HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")


def prepare_manifest(
    content: object,
    *,
    metadata: object,
    finalized_at: object,
    max_size_bytes: int,
) -> Result[tuple[bytes, ArtifactManifest]]:
    if not isinstance(content, bytes):
        return failure(ErrorCode.INVALID_INPUT, "Artifact content must be bytes")
    if len(content) > max_size_bytes:
        return failure(ErrorCode.INVALID_INPUT, "Artifact exceeds size limit")
    if not isinstance(metadata, ArtifactMetadata):
        return failure(ErrorCode.INVALID_INPUT, "Artifact metadata is invalid")
    if not isinstance(finalized_at, datetime):
        return failure(ErrorCode.INVALID_INPUT, "Artifact finalize time is invalid")
    content_hash = hashlib.sha256(content).hexdigest()
    try:
        manifest = ArtifactManifest(
            content_hash=content_hash,
            size_bytes=len(content),
            metadata=metadata,
            finalized_at=finalized_at,
            storage_uri=f"artifact://sha256/{content_hash}",
        )
    except ValidationError:
        return failure(ErrorCode.INVALID_INPUT, "Artifact finalize time is invalid")
    return Success((content, manifest))


def validate_hash(content_hash: object) -> bool:
    return (
        isinstance(content_hash, str)
        and HASH_PATTERN.fullmatch(content_hash) is not None
    )


def failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
