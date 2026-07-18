"""Fail-closed S3 retention, legal-hold, orphan GC, and restore operations."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from stonks_agent.domain.artifact_retention import (
    ArtifactEncryption,
    ArtifactGCDisposition,
    ArtifactGCItem,
    ArtifactGCReport,
    ArtifactGCRequest,
    ArtifactRestoreReceipt,
    ArtifactRestoreRequest,
    ArtifactRetentionMode,
    ArtifactRetentionRequest,
    ArtifactStorageState,
    EnableArtifactLegalHold,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.ports.artifact_store import ArtifactStore

_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")


@runtime_checkable
class S3MaintenanceClientPort(Protocol):
    def list_object_versions(self, **kwargs: object) -> object: ...

    def get_object_retention(self, **kwargs: object) -> object: ...

    def put_object_retention(self, **kwargs: object) -> object: ...

    def get_object_legal_hold(self, **kwargs: object) -> object: ...

    def put_object_legal_hold(self, **kwargs: object) -> object: ...

    def delete_object(self, **kwargs: object) -> object: ...


@dataclass(frozen=True, slots=True)
class _Version:
    key: str
    version_id: str
    modified_at: datetime
    is_latest: bool
    delete_marker: bool


class S3ArtifactMaintenanceBackend:
    """Preserve every finalized artifact; mutate only exact object versions."""

    def __init__(
        self,
        *,
        store: ArtifactStore,
        client: S3MaintenanceClientPort,
        bucket: str,
        prefix: str,
        expected_bucket_owner: str,
        encryption: ArtifactEncryption,
        clock: Callable[[], datetime],
    ) -> None:
        if (
            not _safe_token(bucket)
            or not _safe_prefix(prefix)
            or not re.fullmatch(r"[0-9]{12}", expected_bucket_owner)
            or encryption is ArtifactEncryption.NONE
        ):
            raise ValueError("artifact maintenance configuration is invalid")
        self._store = store
        self._client = client
        self._bucket = bucket
        self._prefix = prefix
        self._owner = expected_bucket_owner
        self._encryption = encryption
        self._clock = clock

    def extend_retention(
        self,
        request: ArtifactRetentionRequest,
    ) -> Result[ArtifactStorageState]:
        observed = self._operation_time(request.requested_at)
        if isinstance(observed, Failure):
            return observed
        versions = self._finalized_versions(request.content_hash)
        if isinstance(versions, Failure):
            return versions
        current = self._retention(versions.value)
        if isinstance(current, Failure):
            return current
        if not _is_extension(current.value, request):
            return _failure(ErrorCode.CONFLICT, "Artifact retention cannot be reduced")
        for version in versions.value:
            updated = self._put_retention(version, request)
            if isinstance(updated, Failure):
                return updated
        return self._state(request.content_hash, versions.value, observed.value)

    def enable_legal_hold(
        self,
        request: EnableArtifactLegalHold,
    ) -> Result[ArtifactStorageState]:
        observed = self._operation_time(request.requested_at)
        if isinstance(observed, Failure):
            return observed
        versions = self._finalized_versions(request.content_hash)
        if isinstance(versions, Failure):
            return versions
        for version in versions.value:
            updated = self._put_legal_hold(version)
            if isinstance(updated, Failure):
                return updated
        return self._state(request.content_hash, versions.value, observed.value)

    def collect_orphans(self, request: ArtifactGCRequest) -> Result[ArtifactGCReport]:
        observed = self._operation_time(request.requested_at)
        if isinstance(observed, Failure):
            return observed
        listed = self._list(
            f"{self._prefix}/objects/",
            maximum=request.max_candidates,
            allow_partial=True,
        )
        if isinstance(listed, Failure):
            return listed
        candidates = tuple(
            version
            for version in listed.value
            if not version.delete_marker and _object_hash(self._prefix, version.key)
        )
        items = tuple(
            self._classify_gc(version, request, observed.value)
            for version in candidates[: request.max_candidates]
        )
        return Success(
            ArtifactGCReport(
                operation_id=request.operation_id,
                cutoff_at=request.cutoff_at,
                scanned=len(items),
                items=items,
                completed_at=observed.value,
            )
        )

    def restore(
        self,
        request: ArtifactRestoreRequest,
    ) -> Result[ArtifactRestoreReceipt]:
        observed = self._operation_time(request.requested_at)
        if isinstance(observed, Failure):
            return observed
        removed = 0
        for key in self._keys(request.content_hash):
            listed = self._list(key, maximum=100)
            if isinstance(listed, Failure):
                return listed
            markers = [
                version
                for version in listed.value
                if version.key == key and version.delete_marker and version.is_latest
            ]
            if len(markers) > 1:
                return _failure(ErrorCode.CONFLICT, "Artifact delete markers conflict")
            if markers:
                deleted = self._delete_exact(markers[0])
                if isinstance(deleted, Failure):
                    return deleted
                removed += 1
        verified = self._store.read(request.content_hash)
        if isinstance(verified, Failure):
            return verified
        return Success(
            ArtifactRestoreReceipt(
                operation_id=request.operation_id,
                content_hash=request.content_hash,
                removed_delete_markers=removed,
                verified=True,
                completed_at=observed.value,
            )
        )

    def _classify_gc(
        self,
        version: _Version,
        request: ArtifactGCRequest,
        observed_at: datetime,
    ) -> ArtifactGCItem:
        content_hash = _object_hash(self._prefix, version.key)
        assert content_hash is not None
        disposition = self._gc_disposition(
            version,
            content_hash,
            request,
            observed_at,
        )
        return ArtifactGCItem(
            content_hash=content_hash,
            version_id=version.version_id,
            disposition=disposition,
        )

    def _gc_disposition(
        self,
        version: _Version,
        content_hash: str,
        request: ArtifactGCRequest,
        observed_at: datetime,
    ) -> ArtifactGCDisposition:
        finalized = self._has_manifest_version(content_hash)
        if finalized is None:
            return ArtifactGCDisposition.RETAINED_UNKNOWN
        if finalized:
            return ArtifactGCDisposition.RETAINED_FINALIZED
        if version.modified_at >= request.cutoff_at:
            return ArtifactGCDisposition.RETAINED_TOO_NEW
        lock = self._version_lock(version)
        if lock is None:
            return ArtifactGCDisposition.RETAINED_UNKNOWN
        retain_until, legal_hold = lock
        if legal_hold or (retain_until is not None and retain_until > observed_at):
            return ArtifactGCDisposition.RETAINED_LOCKED
        deleted = self._delete_exact(version)
        return (
            ArtifactGCDisposition.DELETED
            if isinstance(deleted, Success)
            else ArtifactGCDisposition.RETAINED_UNKNOWN
        )

    def _has_manifest_version(self, content_hash: str) -> bool | None:
        key = self._manifest_key(content_hash)
        listed = self._list(key, maximum=100)
        if isinstance(listed, Failure):
            return None
        return any(
            version.key == key and not version.delete_marker for version in listed.value
        )

    def _finalized_versions(
        self,
        content_hash: str,
    ) -> Result[tuple[_Version, _Version]]:
        verified = self._store.read(content_hash)
        if isinstance(verified, Failure):
            return verified
        found: list[_Version] = []
        for key in self._keys(content_hash):
            current = self._current_version(key)
            if isinstance(current, Failure):
                return current
            found.append(current.value)
        return Success((found[0], found[1]))

    def _current_version(self, key: str) -> Result[_Version]:
        listed = self._list(key, maximum=100)
        if isinstance(listed, Failure):
            return listed
        exact = [
            version
            for version in listed.value
            if version.key == key and version.is_latest
        ]
        if len(exact) != 1 or exact[0].delete_marker:
            return _failure(ErrorCode.CONFLICT, "Artifact version state is invalid")
        return Success(exact[0])

    def _state(
        self,
        content_hash: str,
        versions: tuple[_Version, _Version],
        observed_at: datetime,
    ) -> Result[ArtifactStorageState]:
        retention = self._retention(versions)
        holds = tuple(self._legal_hold(version) for version in versions)
        if (
            isinstance(retention, Failure)
            or any(value is None for value in holds)
            or holds[0] != holds[1]
        ):
            return _failure(ErrorCode.CONFLICT, "Artifact lock state is inconsistent")
        mode, retain_until = retention.value
        return Success(
            ArtifactStorageState(
                content_hash=content_hash,
                finalized=True,
                object_version_id=versions[0].version_id,
                manifest_version_id=versions[1].version_id,
                retention_mode=mode,
                retain_until=retain_until,
                legal_hold=bool(holds[0]),
                encryption=self._encryption,
                observed_at=observed_at,
            )
        )

    def _retention(
        self,
        versions: tuple[_Version, _Version],
    ) -> Result[tuple[ArtifactRetentionMode | None, datetime | None]]:
        values = tuple(self._get_retention(version) for version in versions)
        if any(value is None for value in values) or values[0] != values[1]:
            return _failure(ErrorCode.CONFLICT, "Artifact retention is inconsistent")
        assert values[0] is not None
        return Success(values[0])

    def _version_lock(
        self,
        version: _Version,
    ) -> tuple[datetime | None, bool] | None:
        retention = self._get_retention(version)
        legal_hold = self._legal_hold(version)
        if retention is None or legal_hold is None:
            return None
        return retention[1], legal_hold

    def _get_retention(
        self,
        version: _Version,
    ) -> tuple[ArtifactRetentionMode | None, datetime | None] | None:
        response = self._call(
            "get_object_retention",
            **self._version_request(version),
        )
        if response is None:
            return None
        retention = response.get("Retention")
        if retention in (None, {}):
            return None, None
        if not isinstance(retention, Mapping):
            return None
        mode = _retention_mode(retention.get("Mode"))
        until = _utc(retention.get("RetainUntilDate"))
        return (mode, until) if mode is not None and until is not None else None

    def _legal_hold(self, version: _Version) -> bool | None:
        response = self._call(
            "get_object_legal_hold",
            **self._version_request(version),
        )
        if response is None:
            return None
        hold = response.get("LegalHold")
        if not isinstance(hold, Mapping):
            return None
        status = hold.get("Status")
        return (
            {"ON": True, "OFF": False}.get(status) if isinstance(status, str) else None
        )

    def _put_retention(
        self,
        version: _Version,
        request: ArtifactRetentionRequest,
    ) -> Result[None]:
        response = self._call(
            "put_object_retention",
            **self._version_request(version),
            Retention={
                "Mode": request.mode.value.upper(),
                "RetainUntilDate": request.retain_until,
            },
        )
        return (
            Success(None)
            if response is not None
            else _failure(
                ErrorCode.DATA_UNAVAILABLE, "Artifact retention update failed"
            )
        )

    def _put_legal_hold(self, version: _Version) -> Result[None]:
        response = self._call(
            "put_object_legal_hold",
            **self._version_request(version),
            LegalHold={"Status": "ON"},
        )
        return (
            Success(None)
            if response is not None
            else _failure(
                ErrorCode.DATA_UNAVAILABLE, "Artifact legal hold update failed"
            )
        )

    def _delete_exact(self, version: _Version) -> Result[None]:
        response = self._call("delete_object", **self._version_request(version))
        if response is None:
            return _failure(ErrorCode.DATA_UNAVAILABLE, "Artifact deletion failed")
        listed = self._list(version.key, maximum=100)
        if isinstance(listed, Failure) or any(
            item.key == version.key and item.version_id == version.version_id
            for item in listed.value
        ):
            return _failure(
                ErrorCode.DATA_UNAVAILABLE,
                "Artifact deletion could not be verified",
            )
        return Success(None)

    def _list(
        self,
        prefix: str,
        *,
        maximum: int,
        allow_partial: bool = False,
    ) -> Result[tuple[_Version, ...]]:
        request: dict[str, object] = {
            "Bucket": self._bucket,
            "Prefix": prefix,
            "ExpectedBucketOwner": self._owner,
        }
        found: list[_Version] = []
        seen: set[tuple[str, str]] = set()
        while len(found) < maximum:
            page_max = min(maximum - len(found), 1_000)
            request["MaxKeys"] = page_max
            response = self._call("list_object_versions", **request)
            if response is None:
                return _failure(ErrorCode.DATA_UNAVAILABLE, "Artifact listing failed")
            parsed = _parse_versions(response)
            if (
                parsed is None
                or len(parsed) > page_max
                or any(not version.key.startswith(prefix) for version in parsed)
            ):
                return _failure(
                    ErrorCode.DATA_UNAVAILABLE, "Artifact listing is invalid"
                )
            found.extend(parsed)
            if response.get("IsTruncated") is False:
                return Success(tuple(found))
            if len(found) >= maximum:
                return (
                    Success(tuple(found))
                    if allow_partial
                    else _failure(
                        ErrorCode.DATA_UNAVAILABLE,
                        "Artifact listing exceeded safety limit",
                    )
                )
            marker = _next_markers(response)
            if marker is None or marker in seen:
                return _failure(
                    ErrorCode.DATA_UNAVAILABLE, "Artifact listing is invalid"
                )
            seen.add(marker)
            request["KeyMarker"], request["VersionIdMarker"] = marker
        return Success(tuple(found))

    def _call(self, operation: str, **kwargs: object) -> Mapping[str, object] | None:
        try:
            method = getattr(self._client, operation)
            response = method(**kwargs)
        except Exception:
            return None
        return response if isinstance(response, Mapping) else None

    def _version_request(self, version: _Version) -> dict[str, object]:
        return {
            "Bucket": self._bucket,
            "Key": version.key,
            "VersionId": version.version_id,
            "ExpectedBucketOwner": self._owner,
        }

    def _keys(self, content_hash: str) -> tuple[str, str]:
        return self._object_key(content_hash), self._manifest_key(content_hash)

    def _object_key(self, content_hash: str) -> str:
        return f"{self._prefix}/objects/{content_hash[:2]}/{content_hash}"

    def _manifest_key(self, content_hash: str) -> str:
        return f"{self._prefix}/manifests/{content_hash[:2]}/{content_hash}.json"

    def _operation_time(self, requested_at: datetime) -> Result[datetime]:
        try:
            value = _utc(self._clock())
        except Exception:
            value = None
        if value is None or value < requested_at:
            return _failure(
                ErrorCode.INTERNAL_ERROR,
                "Artifact maintenance clock is invalid",
            )
        return Success(value)


def _parse_versions(response: Mapping[str, object]) -> tuple[_Version, ...] | None:
    parsed: list[_Version] = []
    for field, marker in (("Versions", False), ("DeleteMarkers", True)):
        values = response.get(field, ())
        if not isinstance(values, (list, tuple)):
            return None
        for value in values:
            version = _parse_version(value, delete_marker=marker)
            if version is None:
                return None
            parsed.append(version)
    return tuple(parsed)


def _next_markers(response: Mapping[str, object]) -> tuple[str, str] | None:
    key = response.get("NextKeyMarker")
    version = response.get("NextVersionIdMarker")
    if (
        not isinstance(key, str)
        or not _safe_key(key)
        or not isinstance(version, str)
        or not _safe_version_id(version)
    ):
        return None
    return key, version


def _parse_version(value: object, *, delete_marker: bool) -> _Version | None:
    if not isinstance(value, Mapping):
        return None
    key, version_id = value.get("Key"), value.get("VersionId")
    modified = _utc(value.get("LastModified"))
    latest = value.get("IsLatest")
    if (
        not isinstance(key, str)
        or not _safe_key(key)
        or not isinstance(version_id, str)
        or not _safe_version_id(version_id)
        or modified is None
        or not isinstance(latest, bool)
    ):
        return None
    return _Version(key, version_id, modified, latest, delete_marker)


def _object_hash(prefix: str, key: str) -> str | None:
    start = f"{prefix}/objects/"
    if not key.startswith(start):
        return None
    suffix = key.removeprefix(start)
    parts = suffix.split("/")
    if len(parts) != 2 or len(parts[0]) != 2 or parts[0] != parts[1][:2]:
        return None
    return parts[1] if _HASH_PATTERN.fullmatch(parts[1]) else None


def _is_extension(
    current: tuple[ArtifactRetentionMode | None, datetime | None],
    request: ArtifactRetentionRequest,
) -> bool:
    mode, until = current
    if until is not None and request.retain_until < until:
        return False
    if mode is ArtifactRetentionMode.COMPLIANCE:
        return request.mode is ArtifactRetentionMode.COMPLIANCE
    return True


def _retention_mode(value: object) -> ArtifactRetentionMode | None:
    if not isinstance(value, str):
        return None
    try:
        return ArtifactRetentionMode(value.lower())
    except ValueError:
        return None


def _utc(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        if value.utcoffset() is None:
            return None
        return value.astimezone(UTC)
    except (OverflowError, ValueError):
        return None


def _safe_token(value: str) -> bool:
    return (
        isinstance(value, str)
        and value.isascii()
        and re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,62}", value) is not None
    )


def _safe_prefix(value: str) -> bool:
    return (
        isinstance(value, str)
        and value.isascii()
        and 1 <= len(value) <= 512
        and not value.endswith("/")
        and "//" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _safe_key(value: str) -> bool:
    return (
        value.isascii()
        and 1 <= len(value) <= 1_024
        and "//" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _safe_version_id(value: str) -> bool:
    return (
        value.isascii()
        and 1 <= len(value) <= 1_024
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
