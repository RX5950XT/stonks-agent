"""Atomic local filesystem content-addressed artifact store."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from threading import RLock

from pydantic import ValidationError

from stonks_agent.adapters.artifacts._common import (
    failure,
    prepare_manifest,
    validate_hash,
)
from stonks_agent.adapters.artifacts._file_lock import exclusive_file_lock
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_agent.ports.artifact_store import ArtifactManifest


class LocalArtifactStore:
    def __init__(
        self,
        root: Path,
        *,
        max_size_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        if max_size_bytes < 0:
            raise ValueError("max_size_bytes must be non-negative")
        self._root = Path(root).resolve()
        self._max_size_bytes = max_size_bytes
        self._lock = RLock()
        (self._root / "objects").mkdir(parents=True, exist_ok=True)
        (self._root / "manifests").mkdir(parents=True, exist_ok=True)

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
            try:
                with exclusive_file_lock(self._lock_path(requested.content_hash)):
                    return self._finalize_locked(payload, requested)
            except OSError:
                return failure(ErrorCode.INTERNAL_ERROR, "Artifact finalize failed")

    def _finalize_locked(
        self,
        payload: bytes,
        requested: ArtifactManifest,
    ) -> Result[ArtifactManifest]:
        existing = self.manifest(requested.content_hash)
        if isinstance(existing, Success):
            if existing.value.metadata != requested.metadata:
                return failure(
                    ErrorCode.CONFLICT,
                    "Artifact metadata conflicts with finalized manifest",
                )
            return existing
        if existing.error.code is not ErrorCode.NOT_FOUND:
            return existing
        self._atomic_write(self._object_path(requested.content_hash), payload)
        self._atomic_write(
            self._manifest_path(requested.content_hash),
            _serialize_manifest(requested),
        )
        return Success(requested)

    def read(self, content_hash: str) -> Result[bytes]:
        if not validate_hash(content_hash):
            return failure(ErrorCode.INVALID_INPUT, "Artifact hash is invalid")
        with self._lock:
            manifest = self.manifest(content_hash)
            if isinstance(manifest, Failure):
                return manifest
            try:
                content = self._object_path(content_hash).read_bytes()
            except FileNotFoundError:
                return failure(
                    ErrorCode.CONFLICT, "Finalized artifact object is missing"
                )
            except OSError:
                return failure(ErrorCode.INTERNAL_ERROR, "Artifact read failed")
            if (
                len(content) != manifest.value.size_bytes
                or hashlib.sha256(content).hexdigest() != content_hash
            ):
                return failure(ErrorCode.CONFLICT, "Artifact content hash mismatch")
            return Success(content)

    def manifest(self, content_hash: str) -> Result[ArtifactManifest]:
        if not validate_hash(content_hash):
            return failure(ErrorCode.INVALID_INPUT, "Artifact hash is invalid")
        path = self._manifest_path(content_hash)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return failure(ErrorCode.NOT_FOUND, "Artifact was not finalized")
        except OSError:
            return failure(ErrorCode.INTERNAL_ERROR, "Artifact manifest read failed")
        try:
            manifest = ArtifactManifest.model_validate_json(raw)
        except ValidationError:
            return failure(ErrorCode.CONFLICT, "Artifact manifest is corrupt")
        if manifest.content_hash != content_hash:
            return failure(ErrorCode.CONFLICT, "Artifact manifest hash mismatch")
        return Success(manifest)

    def is_finalized(self, content_hash: str) -> bool:
        manifest = self.manifest(content_hash)
        if isinstance(manifest, Failure):
            return False
        return isinstance(self.read(content_hash), Success)

    def _object_path(self, content_hash: str) -> Path:
        return self._root / "objects" / content_hash[:2] / content_hash

    def _manifest_path(self, content_hash: str) -> Path:
        return self._root / "manifests" / content_hash[:2] / f"{content_hash}.json"

    def _lock_path(self, content_hash: str) -> Path:
        return self._root / "locks" / content_hash[:2] / f"{content_hash}.lock"

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".pending-", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _serialize_manifest(manifest: ArtifactManifest) -> bytes:
    payload = manifest.model_dump(mode="json")
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
