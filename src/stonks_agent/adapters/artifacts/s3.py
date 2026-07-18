"""Immutable S3-compatible artifact storage over an injected typed client."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal, Protocol, Self, runtime_checkable
from urllib.parse import parse_qs, urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    model_validator,
)

from stonks_agent.adapters.artifacts._common import (
    failure,
    prepare_manifest,
    validate_hash,
)
from stonks_agent.domain.artifact_capability import SignedArtifactReadCapability
from stonks_agent.domain.artifact_retention import (
    ArtifactEncryption,
    ArtifactRetentionMode,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_agent.domain.time import normalize_utc
from stonks_agent.ports.artifact_store import ArtifactManifest
from stonks_contracts.common import Sha256
from stonks_contracts.evidence import Sensitivity

_BUCKET_PATTERN = re.compile(
    r"^(?![0-9]+(?:\.[0-9]+){3}$)(?!.*\.\.)(?!.*\.-)(?!.*-\.)"
    r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$"
)
_PREFIX_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,511}$")
_KMS_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./:@-]{0,2047}$")
_MANIFEST_MEDIA_TYPE = "application/vnd.stonks.artifact-manifest+json"
_MAX_MANIFEST_SIZE_BYTES = 65_536
_MAX_ARTIFACT_SIZE_BYTES = 1_073_741_824
_READ_CHUNK_SIZE = 65_536


@runtime_checkable
class S3StreamingBodyPort(Protocol):
    def read(self, amount: int = -1) -> object: ...

    def close(self) -> None: ...


@runtime_checkable
class S3ClientPort(Protocol):
    def put_object(self, **kwargs: object) -> object: ...

    def get_object(self, **kwargs: object) -> object: ...

    def generate_presigned_url(self, **kwargs: object) -> object: ...


class S3ClientError(RuntimeError):
    """Provider-neutral S3 failure exposed by the injected client wrapper."""

    def __init__(
        self,
        *,
        status_code: int | None,
        code: str,
        detail: str | None = None,
    ) -> None:
        del detail
        self.status_code = status_code
        self.code = code
        super().__init__("S3 operation failed")


class StoredArtifactManifestEnvelope(BaseModel):
    """Private deterministic envelope; never changes the canonical manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    object_key: str = Field(
        min_length=1,
        max_length=1_024,
        pattern=r"^[a-z0-9][a-z0-9._/-]*$",
    )
    checksum_sha256: Sha256
    encryption: ArtifactEncryption
    kms_key_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=2_048,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_./:@-]*$",
    )
    retention_mode: ArtifactRetentionMode | None = None
    retain_until: datetime | None = None
    manifest: ArtifactManifest

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.checksum_sha256 != self.manifest.content_hash:
            raise ValueError("stored manifest checksum identity is invalid")
        if (self.encryption is ArtifactEncryption.KMS) != (self.kms_key_id is not None):
            raise ValueError("stored manifest encryption identity is invalid")
        if (self.retention_mode is None) != (self.retain_until is None):
            raise ValueError("stored manifest retention identity is invalid")
        return self


class _PutOutcome(StrEnum):
    CREATED = "created"
    EXISTS = "exists"


class S3ArtifactStore:
    """Content-addressed bytes with manifest-last conditional publication."""

    def __init__(
        self,
        *,
        client: S3ClientPort,
        bucket: str,
        prefix: str,
        kms_key_id: str | None,
        encryption: ArtifactEncryption = ArtifactEncryption.KMS,
        retention: Mapping[Sensitivity, tuple[ArtifactRetentionMode, int]]
        | None = None,
        expected_bucket_owner: str | None = None,
        max_size_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        _validate_configuration(
            bucket=bucket,
            prefix=prefix,
            kms_key_id=kms_key_id,
            encryption=encryption,
            retention=retention,
            expected_bucket_owner=expected_bucket_owner,
            max_size_bytes=max_size_bytes,
        )
        self._client = client
        self._bucket = bucket
        self._prefix = prefix
        self._kms_key_id = kms_key_id
        self._encryption = encryption
        self._retention = dict(retention) if retention is not None else None
        self._expected_bucket_owner = expected_bucket_owner
        self._max_size_bytes = max_size_bytes

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
        existing = self._read_envelope(requested.content_hash)
        if isinstance(existing, Success):
            return self._reconcile(requested, existing.value)
        if existing.error.code is not ErrorCode.NOT_FOUND:
            return existing
        stored = self._ensure_object(payload, requested)
        if isinstance(stored, Failure):
            return stored
        return self._publish_manifest(requested)

    def read(self, content_hash: str) -> Result[bytes]:
        if not validate_hash(content_hash):
            return failure(ErrorCode.INVALID_INPUT, "Artifact hash is invalid")
        envelope = self._read_envelope(content_hash)
        if isinstance(envelope, Failure):
            return envelope
        return self._read_object(envelope.value.manifest)

    def manifest(self, content_hash: str) -> Result[ArtifactManifest]:
        if not validate_hash(content_hash):
            return failure(ErrorCode.INVALID_INPUT, "Artifact hash is invalid")
        envelope = self._read_envelope(content_hash)
        if isinstance(envelope, Failure):
            return envelope
        return Success(envelope.value.manifest)

    def is_finalized(self, content_hash: str) -> bool:
        return isinstance(self.read(content_hash), Success)

    def _ensure_object(
        self,
        payload: bytes,
        manifest: ArtifactManifest,
    ) -> Result[None]:
        outcome = self._put(
            key=self._object_key(manifest.content_hash),
            body=payload,
            content_type=manifest.metadata.media_type,
            kind="object",
            content_hash=manifest.content_hash,
            retention=self._retention_for(manifest),
        )
        if isinstance(outcome, Failure):
            return outcome
        verified = self._read_object(manifest)
        if isinstance(verified, Failure):
            return verified
        return Success(None)

    def _publish_manifest(
        self,
        requested: ArtifactManifest,
    ) -> Result[ArtifactManifest]:
        envelope = StoredArtifactManifestEnvelope(
            object_key=self._object_key(requested.content_hash),
            checksum_sha256=requested.content_hash,
            encryption=self._encryption,
            kms_key_id=self._kms_key_id,
            retention_mode=self._retention_for(requested)[0],
            retain_until=self._retention_for(requested)[1],
            manifest=requested,
        )
        encoded = _serialize_envelope(envelope)
        if len(encoded) > _MAX_MANIFEST_SIZE_BYTES:
            return failure(
                ErrorCode.INVALID_INPUT, "Artifact manifest exceeds size limit"
            )
        outcome = self._put(
            key=self._manifest_key(requested.content_hash),
            body=encoded,
            content_type=_MANIFEST_MEDIA_TYPE,
            kind="manifest",
            content_hash=requested.content_hash,
            retention=self._retention_for(requested),
        )
        if isinstance(outcome, Failure):
            return outcome
        published = self._read_envelope(requested.content_hash)
        if isinstance(published, Failure):
            return published
        return self._reconcile(requested, published.value)

    def _reconcile(
        self,
        requested: ArtifactManifest,
        existing: StoredArtifactManifestEnvelope,
    ) -> Result[ArtifactManifest]:
        if existing.manifest.metadata != requested.metadata:
            return failure(
                ErrorCode.CONFLICT,
                "Artifact metadata conflicts with finalized manifest",
            )
        verified = self._read_object(existing.manifest)
        if isinstance(verified, Failure):
            return verified
        return Success(existing.manifest)

    def _put(
        self,
        *,
        key: str,
        body: bytes,
        content_type: str,
        kind: str,
        content_hash: str,
        retention: tuple[ArtifactRetentionMode | None, datetime | None],
    ) -> Result[_PutOutcome]:
        request = self._request(key)
        request.update(
            {
                "Body": body,
                "ContentType": content_type,
                "Metadata": _stored_metadata(kind, content_hash),
                "ChecksumSHA256": _checksum(body),
                "IfNoneMatch": "*",
            }
        )
        request.update(self._encryption_request())
        request.update(self._retention_request(retention))
        try:
            response = self._client.put_object(**request)
        except S3ClientError as error:
            if _is_precondition_failure(error):
                return Success(_PutOutcome.EXISTS)
            return _internal_failure("Artifact storage write failed")
        except Exception:
            return _internal_failure("Artifact storage write failed")
        if not isinstance(response, Mapping):
            return _internal_failure("Artifact storage write failed")
        return Success(_PutOutcome.CREATED)

    def _read_envelope(
        self,
        content_hash: str,
    ) -> Result[StoredArtifactManifestEnvelope]:
        response = self._get(self._manifest_key(content_hash))
        if isinstance(response, Failure):
            return response
        payload = self._consume(
            response.value,
            kind="manifest",
            content_hash=content_hash,
            content_type=_MANIFEST_MEDIA_TYPE,
            maximum=_MAX_MANIFEST_SIZE_BYTES,
            exact_size=None,
            verify_content_hash=False,
            retention_mode=None,
            retain_until=None,
        )
        if isinstance(payload, Failure):
            return payload
        parsed = self._parse_envelope(payload.value, content_hash)
        if isinstance(parsed, Failure):
            return parsed
        invalid = self._validate_retention_response(
            response.value,
            parsed.value.retention_mode,
            parsed.value.retain_until,
        )
        return invalid or parsed

    def _parse_envelope(
        self,
        payload: bytes,
        content_hash: str,
    ) -> Result[StoredArtifactManifestEnvelope]:
        try:
            envelope = StoredArtifactManifestEnvelope.model_validate_json(payload)
        except ValidationError:
            return failure(ErrorCode.CONFLICT, "Artifact manifest is corrupt")
        if (
            envelope.manifest.content_hash != content_hash
            or envelope.object_key != self._object_key(content_hash)
            or envelope.encryption is not self._encryption
            or envelope.kms_key_id != self._kms_key_id
            or envelope.manifest.size_bytes > self._max_size_bytes
            or (
                envelope.retention_mode,
                envelope.retain_until,
            )
            != self._retention_for(envelope.manifest)
        ):
            return failure(ErrorCode.CONFLICT, "Artifact manifest identity mismatch")
        return Success(envelope)

    def _read_object(self, manifest: ArtifactManifest) -> Result[bytes]:
        response = self._get(self._object_key(manifest.content_hash))
        if isinstance(response, Failure):
            if response.error.code is ErrorCode.NOT_FOUND:
                return failure(
                    ErrorCode.CONFLICT,
                    "Finalized artifact object is missing",
                )
            return response
        return self._consume(
            response.value,
            kind="object",
            content_hash=manifest.content_hash,
            content_type=manifest.metadata.media_type,
            maximum=self._max_size_bytes,
            exact_size=manifest.size_bytes,
            verify_content_hash=True,
            retention_mode=self._retention_for(manifest)[0],
            retain_until=self._retention_for(manifest)[1],
        )

    def _get(self, key: str) -> Result[Mapping[str, object]]:
        try:
            response = self._client.get_object(
                **self._request(key),
                ChecksumMode="ENABLED",
            )
        except S3ClientError as error:
            if _is_not_found(error):
                return failure(ErrorCode.NOT_FOUND, "Artifact was not finalized")
            return _internal_failure("Artifact storage read failed")
        except Exception:
            return _internal_failure("Artifact storage read failed")
        if not isinstance(response, Mapping):
            return _internal_failure("Artifact storage read failed")
        return Success(response)

    def _consume(
        self,
        response: Mapping[str, object],
        *,
        kind: str,
        content_hash: str,
        content_type: str,
        maximum: int,
        exact_size: int | None,
        verify_content_hash: bool,
        retention_mode: ArtifactRetentionMode | None,
        retain_until: datetime | None,
    ) -> Result[bytes]:
        body = response.get("Body")
        if not isinstance(body, S3StreamingBodyPort):
            return _internal_failure("Artifact storage response is invalid")
        result = self._consume_open_body(
            response,
            body,
            kind=kind,
            content_hash=content_hash,
            content_type=content_type,
            maximum=maximum,
            exact_size=exact_size,
            verify_content_hash=verify_content_hash,
            retention_mode=retention_mode,
            retain_until=retain_until,
        )
        try:
            body.close()
        except Exception:
            if isinstance(result, Success):
                return _internal_failure("Artifact storage response close failed")
        return result

    def _consume_open_body(
        self,
        response: Mapping[str, object],
        body: S3StreamingBodyPort,
        *,
        kind: str,
        content_hash: str,
        content_type: str,
        maximum: int,
        exact_size: int | None,
        verify_content_hash: bool,
        retention_mode: ArtifactRetentionMode | None,
        retain_until: datetime | None,
    ) -> Result[bytes]:
        try:
            invalid = self._validate_response(
                response,
                kind=kind,
                content_hash=content_hash,
                content_type=content_type,
                maximum=maximum,
                exact_size=exact_size,
                retention_mode=retention_mode,
                retain_until=retain_until,
            )
        except Exception:
            return _internal_failure("Artifact storage response is invalid")
        if invalid is not None:
            return invalid
        payload = _read_bounded(body, maximum)
        if isinstance(payload, Failure):
            return payload
        expected_checksum = response.get("ChecksumSHA256")
        if expected_checksum != _checksum(payload.value):
            return failure(ErrorCode.CONFLICT, "Artifact checksum mismatch")
        if verify_content_hash and _hex_digest(payload.value) != content_hash:
            return failure(ErrorCode.CONFLICT, "Artifact content hash mismatch")
        return payload

    def _validate_response(
        self,
        response: Mapping[str, object],
        *,
        kind: str,
        content_hash: str,
        content_type: str,
        maximum: int,
        exact_size: int | None,
        retention_mode: ArtifactRetentionMode | None,
        retain_until: datetime | None,
    ) -> Failure | None:
        size = response.get("ContentLength")
        metadata = response.get("Metadata")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or size > maximum
            or (exact_size is not None and size != exact_size)
            or response.get("ContentType") != content_type
            or not isinstance(metadata, Mapping)
            or metadata.get("stonks-kind") != kind
            or metadata.get("stonks-sha256") != content_hash
            or response.get("ServerSideEncryption") != self._encryption.value
            or response.get("SSEKMSKeyId") != self._kms_key_id
            or response.get("ContentEncoding") not in {None, "identity"}
        ):
            return failure(ErrorCode.CONFLICT, "Artifact storage identity mismatch")
        return self._validate_retention_response(
            response,
            retention_mode,
            retain_until,
        )

    def _validate_retention_response(
        self,
        response: Mapping[str, object],
        mode: ArtifactRetentionMode | None,
        retain_until: datetime | None,
    ) -> Failure | None:
        if mode is None or retain_until is None:
            return (
                None
                if mode is None and retain_until is None
                else failure(
                    ErrorCode.CONFLICT,
                    "Artifact retention identity mismatch",
                )
            )
        actual_mode = response.get("ObjectLockMode")
        normalized = normalize_utc(response.get("ObjectLockRetainUntilDate"))
        actual_until = normalized.value if isinstance(normalized, Success) else None
        if actual_mode != mode.value.upper() or actual_until != retain_until:
            return failure(ErrorCode.CONFLICT, "Artifact retention identity mismatch")
        return None

    def _encryption_request(self) -> dict[str, object]:
        if self._encryption is ArtifactEncryption.AES256:
            return {"ServerSideEncryption": "AES256"}
        assert self._kms_key_id is not None
        return {
            "ServerSideEncryption": "aws:kms",
            "SSEKMSKeyId": self._kms_key_id,
            "BucketKeyEnabled": True,
        }

    def _retention_request(
        self,
        retention: tuple[ArtifactRetentionMode | None, datetime | None],
    ) -> dict[str, object]:
        mode, until = retention
        if mode is None or until is None:
            return {}
        return {
            "ObjectLockMode": mode.value.upper(),
            "ObjectLockRetainUntilDate": until,
        }

    def _retention_for(
        self,
        manifest: ArtifactManifest,
    ) -> tuple[ArtifactRetentionMode | None, datetime | None]:
        if self._retention is None:
            return None, None
        mode, days = self._retention[manifest.metadata.sensitivity]
        return mode, manifest.finalized_at + timedelta(days=days)

    def _request(self, key: str) -> dict[str, object]:
        request: dict[str, object] = {"Bucket": self._bucket, "Key": key}
        if self._expected_bucket_owner is not None:
            request["ExpectedBucketOwner"] = self._expected_bucket_owner
        return request

    def _object_key(self, content_hash: str) -> str:
        return f"{self._prefix}/objects/{content_hash[:2]}/{content_hash}"

    def _manifest_key(self, content_hash: str) -> str:
        return f"{self._prefix}/manifests/{content_hash[:2]}/{content_hash}.json"


class S3ReadCapabilityIssuer:
    """Issue one exact, short-lived GET capability after full verification."""

    def __init__(
        self,
        *,
        store: S3ArtifactStore,
        client: S3ClientPort,
        clock: Callable[[], datetime],
        allowed_origin: str,
        max_ttl_seconds: int = 300,
    ) -> None:
        self._store = store
        self._client = client
        self._clock = clock
        self._origin = _validate_origin(allowed_origin)
        if not 1 <= max_ttl_seconds <= 900:
            raise ValueError("artifact capability TTL is invalid")
        self._max_ttl_seconds = max_ttl_seconds

    def issue_read_url(
        self,
        content_hash: str,
        *,
        expires_at: object,
    ) -> Result[SignedArtifactReadCapability]:
        if not validate_hash(content_hash):
            return failure(ErrorCode.INVALID_INPUT, "Artifact hash is invalid")
        expiry = normalize_utc(expires_at)
        if isinstance(expiry, Failure):
            return expiry
        current = normalize_utc(self._clock())
        if isinstance(current, Failure):
            return _internal_failure("Artifact capability clock is invalid")
        ttl = int((expiry.value - current.value).total_seconds())
        if ttl < 1 or ttl > self._max_ttl_seconds:
            return failure(
                ErrorCode.INVALID_INPUT, "Artifact capability TTL is invalid"
            )
        verified = self._store.read(content_hash)
        if isinstance(verified, Failure):
            return verified
        url = self._presign(content_hash, ttl)
        if isinstance(url, Failure):
            return url
        effective_expiry = current.value + timedelta(seconds=ttl)
        try:
            capability = SignedArtifactReadCapability(
                content_hash=content_hash,
                url=SecretStr(url.value),
                expires_at=effective_expiry,
            )
        except ValidationError:
            return _internal_failure("Artifact capability response is invalid")
        return Success(capability)

    def _presign(self, content_hash: str, ttl: int) -> Result[str]:
        key = self._store._object_key(content_hash)
        try:
            url = self._client.generate_presigned_url(
                ClientMethod="get_object",
                Params={"Bucket": self._store._bucket, "Key": key},
                ExpiresIn=ttl,
                HttpMethod="GET",
            )
        except Exception:
            return _internal_failure("Artifact capability issuance failed")
        if not isinstance(url, str) or not self._valid_url(url, key, ttl):
            return _internal_failure("Artifact capability response is invalid")
        return Success(url)

    def _valid_url(self, value: str, key: str, ttl: int) -> bool:
        parsed = urlsplit(value)
        query = parse_qs(parsed.query, keep_blank_values=True)
        actual_origin = f"{parsed.scheme}://{parsed.netloc}"
        return (
            20 <= len(value) <= 4_096
            and value.isascii()
            and actual_origin == self._origin
            and parsed.path == f"/{self._store._bucket}/{key}"
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and len(query.get("X-Amz-Signature", ())) == 1
            and 32 <= len(query["X-Amz-Signature"][0]) <= 512
            and query.get("X-Amz-Expires") == [str(ttl)]
        )


def _validate_configuration(
    *,
    bucket: str,
    prefix: str,
    kms_key_id: str | None,
    encryption: ArtifactEncryption,
    retention: Mapping[Sensitivity, tuple[ArtifactRetentionMode, int]] | None,
    expected_bucket_owner: str | None,
    max_size_bytes: int,
) -> None:
    if not isinstance(bucket, str) or _BUCKET_PATTERN.fullmatch(bucket) is None:
        raise ValueError("artifact bucket is invalid")
    if (
        not isinstance(prefix, str)
        or _PREFIX_PATTERN.fullmatch(prefix) is None
        or prefix.endswith("/")
        or "//" in prefix
        or any(part in {".", ".."} for part in prefix.split("/"))
    ):
        raise ValueError("artifact prefix is invalid")
    if encryption not in {ArtifactEncryption.KMS, ArtifactEncryption.AES256}:
        raise ValueError("artifact encryption is invalid")
    valid_kms_key = (
        isinstance(kms_key_id, str)
        and _KMS_KEY_PATTERN.fullmatch(kms_key_id) is not None
    )
    if (encryption is ArtifactEncryption.KMS) != valid_kms_key:
        raise ValueError("artifact encryption key is invalid")
    if retention is not None and not _valid_retention(retention):
        raise ValueError("artifact retention policy is invalid")
    if expected_bucket_owner is not None and (
        not isinstance(expected_bucket_owner, str)
        or len(expected_bucket_owner) != 12
        or not expected_bucket_owner.isascii()
        or not expected_bucket_owner.isdecimal()
    ):
        raise ValueError("artifact bucket owner is invalid")
    if (
        isinstance(max_size_bytes, bool)
        or not isinstance(max_size_bytes, int)
        or not 0 <= max_size_bytes <= _MAX_ARTIFACT_SIZE_BYTES
    ):
        raise ValueError("artifact size limit is invalid")


def _valid_retention(
    retention: Mapping[Sensitivity, tuple[ArtifactRetentionMode, int]],
) -> bool:
    if set(retention) != set(Sensitivity):
        return False
    for configured in retention.values():
        entry: object = configured
        if not isinstance(entry, tuple) or len(entry) != 2:
            return False
        mode, days = entry
        if (
            not isinstance(mode, ArtifactRetentionMode)
            or isinstance(days, bool)
            or not isinstance(days, int)
            or not 1 <= days <= 36_500
        ):
            return False
    return True


def _validate_origin(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("artifact capability origin is invalid")
    parsed = urlsplit(value)
    if (
        not isinstance(value, str)
        or not value.isascii()
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("artifact capability origin is invalid")
    return value.rstrip("/")


def _serialize_envelope(envelope: StoredArtifactManifestEnvelope) -> bytes:
    return json.dumps(
        envelope.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _stored_metadata(kind: str, content_hash: str) -> dict[str, str]:
    return {"stonks-kind": kind, "stonks-sha256": content_hash}


def _checksum(value: bytes) -> str:
    return base64.b64encode(hashlib.sha256(value).digest()).decode("ascii")


def _hex_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_bounded(
    body: S3StreamingBodyPort,
    maximum: int,
) -> Result[bytes]:
    payload = bytearray()
    try:
        while True:
            remaining = maximum - len(payload)
            chunk = body.read(min(_READ_CHUNK_SIZE, remaining + 1))
            if not isinstance(chunk, bytes):
                return _internal_failure("Artifact storage response is invalid")
            if not chunk:
                break
            if len(chunk) > remaining:
                return failure(ErrorCode.CONFLICT, "Artifact exceeds stored size")
            payload.extend(chunk)
    except Exception:
        return _internal_failure("Artifact storage body read failed")
    return Success(bytes(payload))


def _is_precondition_failure(error: S3ClientError) -> bool:
    return error.status_code == 412 or error.code == "PreconditionFailed"


def _is_not_found(error: S3ClientError) -> bool:
    return error.status_code == 404 or error.code in {"NoSuchKey", "NotFound"}


def _internal_failure(message: str) -> Failure:
    return failure(ErrorCode.INTERNAL_ERROR, message)
