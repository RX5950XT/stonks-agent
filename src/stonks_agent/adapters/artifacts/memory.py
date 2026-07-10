"""Thread-safe in-memory content-addressed artifact store."""

from __future__ import annotations

import hashlib
from threading import RLock

from stonks_agent.adapters.artifacts._common import (
    failure,
    prepare_manifest,
    validate_hash,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_agent.ports.artifact_store import ArtifactManifest


class MemoryArtifactStore:
    def __init__(self, *, max_size_bytes: int = 64 * 1024 * 1024) -> None:
        if max_size_bytes < 0:
            raise ValueError("max_size_bytes must be non-negative")
        self._max_size_bytes = max_size_bytes
        self._objects: dict[str, bytes] = {}
        self._manifests: dict[str, ArtifactManifest] = {}
        self._lock = RLock()

    def finalize(
        self,
        content: object,
        *,
        metadata: object,
        finalized_at: object,
    ) -> Result[ArtifactManifest]:
        prepared = prepare_manifest(
            content,
            metadata=metadata,
            finalized_at=finalized_at,
            max_size_bytes=self._max_size_bytes,
        )
        if isinstance(prepared, Failure):
            return prepared
        payload, requested = prepared.value
        with self._lock:
            existing = self._manifests.get(requested.content_hash)
            if existing is not None:
                if existing.metadata != requested.metadata:
                    return failure(
                        ErrorCode.CONFLICT,
                        "Artifact metadata conflicts with finalized manifest",
                    )
                return Success(existing)
            self._objects[requested.content_hash] = payload
            self._manifests[requested.content_hash] = requested
            return Success(requested)

    def read(self, content_hash: str) -> Result[bytes]:
        if not validate_hash(content_hash):
            return failure(ErrorCode.INVALID_INPUT, "Artifact hash is invalid")
        with self._lock:
            manifest = self._manifests.get(content_hash)
            content = self._objects.get(content_hash)
            if manifest is None or content is None:
                return failure(ErrorCode.NOT_FOUND, "Artifact was not finalized")
            if (
                len(content) != manifest.size_bytes
                or hashlib.sha256(content).hexdigest() != content_hash
            ):
                return failure(ErrorCode.CONFLICT, "Artifact content hash mismatch")
            return Success(content)

    def manifest(self, content_hash: str) -> Result[ArtifactManifest]:
        if not validate_hash(content_hash):
            return failure(ErrorCode.INVALID_INPUT, "Artifact hash is invalid")
        with self._lock:
            value = self._manifests.get(content_hash)
            if value is None:
                return failure(ErrorCode.NOT_FOUND, "Artifact was not finalized")
            return Success(value)

    def is_finalized(self, content_hash: str) -> bool:
        return isinstance(self.manifest(content_hash), Success)
