from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import RLock

import pytest
from pydantic import SecretStr

from stonks_agent.adapters.artifacts.s3 import (
    S3ArtifactStore,
    S3ClientError,
    S3ReadCapabilityIssuer,
)
from stonks_agent.domain.artifact import ArtifactMetadata
from stonks_agent.domain.artifact_retention import (
    ArtifactEncryption,
    ArtifactRetentionMode,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_agent.ports.artifact_capability import ArtifactReadCapabilityIssuerPort
from stonks_agent.ports.artifact_store import ArtifactManifest, ArtifactStore
from stonks_contracts.evidence import Sensitivity

NOW = datetime(2026, 7, 17, 12, tzinfo=UTC)
CONTENT = b'{"symbol":"AAPL","close":"100.00"}'
HASH = hashlib.sha256(CONTENT).hexdigest()
BUCKET = "stonks-artifacts"
PREFIX = "prod/artifacts"
KMS_KEY = "arn:aws:kms:us-east-1:123456789012:key/test"
OWNER = "123456789012"
ORIGIN = "https://objects.example"


class FakeBody:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self._offset = 0
        self.closed = False

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            amount = len(self._content) - self._offset
        start = self._offset
        self._offset = min(len(self._content), self._offset + amount)
        return self._content[start : self._offset]

    def close(self) -> None:
        self.closed = True


@dataclass
class Stored:
    body: bytes
    content_type: str
    metadata: dict[str, str]
    checksum: str
    encryption: str
    kms_key_id: str | None
    retention_mode: str | None
    retain_until: datetime | None
    content_length: int


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, Stored] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.fail_manifest_once = False
        self.get_error: S3ClientError | None = None
        self.presigned_url: str | None = None
        self._lock = RLock()

    def put_object(self, **kwargs: object) -> Mapping[str, object]:
        with self._lock:
            self.calls.append(("put_object", dict(kwargs)))
            key = _string(kwargs["Key"])
            if self.fail_manifest_once and "/manifests/" in key:
                self.fail_manifest_once = False
                raise S3ClientError(status_code=500, code="InternalError")
            if key in self.objects and kwargs.get("IfNoneMatch") == "*":
                raise S3ClientError(status_code=412, code="PreconditionFailed")
            body = kwargs["Body"]
            assert isinstance(body, bytes)
            metadata = kwargs["Metadata"]
            assert isinstance(metadata, dict)
            self.objects[key] = Stored(
                body=body,
                content_type=_string(kwargs["ContentType"]),
                metadata={str(name): str(value) for name, value in metadata.items()},
                checksum=_string(kwargs["ChecksumSHA256"]),
                encryption=_string(kwargs["ServerSideEncryption"]),
                kms_key_id=(
                    _string(kwargs["SSEKMSKeyId"]) if "SSEKMSKeyId" in kwargs else None
                ),
                retention_mode=(
                    _string(kwargs["ObjectLockMode"])
                    if "ObjectLockMode" in kwargs
                    else None
                ),
                retain_until=(
                    kwargs["ObjectLockRetainUntilDate"]
                    if isinstance(kwargs.get("ObjectLockRetainUntilDate"), datetime)
                    else None
                ),
                content_length=len(body),
            )
            return {"ChecksumSHA256": self.objects[key].checksum}

    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        with self._lock:
            self.calls.append(("get_object", dict(kwargs)))
            if self.get_error is not None:
                error = self.get_error
                self.get_error = None
                raise error
            key = _string(kwargs["Key"])
            stored = self.objects.get(key)
            if stored is None:
                raise S3ClientError(status_code=404, code="NoSuchKey")
            return {
                "Body": FakeBody(stored.body),
                "ContentLength": stored.content_length,
                "ContentType": stored.content_type,
                "Metadata": stored.metadata,
                "ChecksumSHA256": stored.checksum,
                "ServerSideEncryption": stored.encryption,
                "SSEKMSKeyId": stored.kms_key_id,
                "ObjectLockMode": stored.retention_mode,
                "ObjectLockRetainUntilDate": stored.retain_until,
            }

    def generate_presigned_url(self, **kwargs: object) -> str:
        self.calls.append(("generate_presigned_url", dict(kwargs)))
        if self.presigned_url is not None:
            return self.presigned_url
        params = kwargs["Params"]
        assert isinstance(params, dict)
        key = _string(params["Key"])
        expires = int(kwargs["ExpiresIn"])
        return (
            f"{ORIGIN}/{BUCKET}/{key}?X-Amz-Expires={expires}"
            "&X-Amz-Signature=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        )


def _string(value: object) -> str:
    assert isinstance(value, str)
    return value


def metadata(*, license_tag: str = "Apache-2.0") -> ArtifactMetadata:
    return ArtifactMetadata(
        media_type="application/json",
        license_tag=license_tag,
        sensitivity=Sensitivity.INTERNAL,
        source="test",
        attributes=(("schema", "test/1.0.0"),),
    )


def store(
    client: FakeS3Client,
    *,
    max_size_bytes: int = 1024,
    encryption: ArtifactEncryption = ArtifactEncryption.KMS,
    kms_key_id: str | None = KMS_KEY,
    retention: dict[Sensitivity, tuple[ArtifactRetentionMode, int]] | None = None,
) -> S3ArtifactStore:
    value = S3ArtifactStore(
        client=client,
        bucket=BUCKET,
        prefix=PREFIX,
        kms_key_id=kms_key_id,
        encryption=encryption,
        retention=retention,
        expected_bucket_owner=OWNER,
        max_size_bytes=max_size_bytes,
    )
    assert isinstance(value, ArtifactStore)
    return value


def unwrap[T](result: Result[T]) -> T:
    assert isinstance(result, Success)
    return result.value


def object_key() -> str:
    return f"{PREFIX}/objects/{HASH[:2]}/{HASH}"


def manifest_key() -> str:
    return f"{PREFIX}/manifests/{HASH[:2]}/{HASH}.json"


def test_finalize_publishes_manifest_last_and_round_trips_exact_contract() -> None:
    client = FakeS3Client()
    artifacts = store(client)

    manifest = unwrap(
        artifacts.finalize(CONTENT, metadata=metadata(), finalized_at=NOW)
    )

    assert manifest.content_hash == HASH
    assert manifest.storage_uri == f"artifact://sha256/{HASH}"
    assert unwrap(artifacts.manifest(HASH)) == manifest
    assert unwrap(artifacts.read(HASH)) == CONTENT
    assert artifacts.is_finalized(HASH)
    puts = tuple(call for call in client.calls if call[0] == "put_object")
    assert tuple(call[1]["Key"] for call in puts) == (object_key(), manifest_key())
    for _, request in puts:
        assert request["IfNoneMatch"] == "*"
        assert request["ServerSideEncryption"] == "aws:kms"
        assert request["SSEKMSKeyId"] == KMS_KEY
        assert request["ExpectedBucketOwner"] == OWNER
        assert request["BucketKeyEnabled"] is True
    expected_checksum = base64.b64encode(hashlib.sha256(CONTENT).digest()).decode()
    assert puts[0][1]["ChecksumSHA256"] == expected_checksum
    envelope = json.loads(client.objects[manifest_key()].body)
    assert envelope["schema_version"] == 1
    assert envelope["object_key"] == object_key()
    assert envelope["manifest"]["storage_uri"] == f"artifact://sha256/{HASH}"


def test_finalize_applies_sensitivity_retention_and_aes256_to_both_versions() -> None:
    client = FakeS3Client()
    policy = {
        Sensitivity.PUBLIC: (ArtifactRetentionMode.GOVERNANCE, 30),
        Sensitivity.INTERNAL: (ArtifactRetentionMode.GOVERNANCE, 365),
        Sensitivity.RESTRICTED: (ArtifactRetentionMode.COMPLIANCE, 2_555),
    }
    artifacts = store(
        client,
        encryption=ArtifactEncryption.AES256,
        kms_key_id=None,
        retention=policy,
    )

    result = artifacts.finalize(CONTENT, metadata=metadata(), finalized_at=NOW)

    assert isinstance(result, Success)
    puts = tuple(call for call in client.calls if call[0] == "put_object")
    assert len(puts) == 2
    for _, request in puts:
        assert request["ServerSideEncryption"] == "AES256"
        assert "SSEKMSKeyId" not in request
        assert "BucketKeyEnabled" not in request
        assert request["ObjectLockMode"] == "GOVERNANCE"
        assert request["ObjectLockRetainUntilDate"] == NOW + timedelta(days=365)
    envelope = json.loads(client.objects[manifest_key()].body)
    assert envelope["encryption"] == "AES256"
    assert envelope["kms_key_id"] is None
    assert envelope["retention_mode"] == "governance"
    assert envelope["retain_until"] == (NOW + timedelta(days=365)).isoformat().replace(
        "+00:00", "Z"
    )


def test_read_rejects_encryption_or_retention_drift() -> None:
    client = FakeS3Client()
    policy = {
        sensitivity: (ArtifactRetentionMode.COMPLIANCE, 30)
        for sensitivity in Sensitivity
    }
    artifacts = store(client, retention=policy)
    unwrap(artifacts.finalize(CONTENT, metadata=metadata(), finalized_at=NOW))

    client.objects[object_key()].retention_mode = "GOVERNANCE"
    retention_drift = artifacts.read(HASH)
    client.objects[object_key()].retention_mode = "COMPLIANCE"
    client.objects[object_key()].kms_key_id = "alias/other-key"
    encryption_drift = artifacts.read(HASH)

    assert isinstance(retention_drift, Failure)
    assert retention_drift.error.code is ErrorCode.CONFLICT
    assert isinstance(encryption_drift, Failure)
    assert encryption_drift.error.code is ErrorCode.CONFLICT


def test_retry_preserves_first_manifest_and_conflicting_metadata_fails() -> None:
    client = FakeS3Client()
    artifacts = store(client)
    first = unwrap(artifacts.finalize(CONTENT, metadata=metadata(), finalized_at=NOW))
    retried = unwrap(
        artifacts.finalize(
            CONTENT,
            metadata=metadata(),
            finalized_at=NOW + timedelta(days=1),
        )
    )
    conflict = artifacts.finalize(
        CONTENT,
        metadata=metadata(license_tag="different"),
        finalized_at=NOW,
    )

    assert retried == first
    assert retried.finalized_at == NOW
    assert isinstance(conflict, Failure)
    assert conflict.error.code is ErrorCode.CONFLICT


def test_partial_object_write_is_reconciled_on_retry() -> None:
    client = FakeS3Client()
    client.fail_manifest_once = True
    artifacts = store(client)

    failed = artifacts.finalize(CONTENT, metadata=metadata(), finalized_at=NOW)
    assert isinstance(failed, Failure)
    assert failed.error.code is ErrorCode.INTERNAL_ERROR
    assert object_key() in client.objects
    assert manifest_key() not in client.objects

    recovered = unwrap(
        artifacts.finalize(CONTENT, metadata=metadata(), finalized_at=NOW)
    )
    assert recovered.content_hash == HASH
    assert unwrap(artifacts.read(HASH)) == CONTENT


def test_concurrent_conflicting_finalize_has_one_metadata_identity() -> None:
    client = FakeS3Client()
    artifacts = store(client)

    def finalize(index: int) -> Result[ArtifactManifest]:
        return artifacts.finalize(
            CONTENT,
            metadata=metadata(license_tag=f"license-{index % 2}"),
            finalized_at=NOW + timedelta(seconds=index),
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(executor.map(finalize, range(16)))

    successes = tuple(item.value for item in results if isinstance(item, Success))
    failures = tuple(item for item in results if isinstance(item, Failure))
    assert successes
    assert len({item.metadata for item in successes}) == 1
    assert failures
    assert all(item.error.code is ErrorCode.CONFLICT for item in failures)


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    (
        ("body", b"corrupt"),
        ("content_length", len(CONTENT) + 1),
        ("content_type", "text/plain"),
        ("checksum", base64.b64encode(b"x" * 32).decode()),
        ("encryption", "AES256"),
        ("kms_key_id", "different-key"),
    ),
)
def test_read_rejects_corrupt_object_identity(
    field: str,
    unsafe_value: object,
) -> None:
    client = FakeS3Client()
    artifacts = store(client)
    unwrap(artifacts.finalize(CONTENT, metadata=metadata(), finalized_at=NOW))
    setattr(client.objects[object_key()], field, unsafe_value)

    result = artifacts.read(HASH)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT


def test_invalid_oversize_missing_and_backend_failures_are_safe() -> None:
    client = FakeS3Client()
    artifacts = store(client, max_size_bytes=len(CONTENT))

    invalid = artifacts.read("../../secret")
    missing = artifacts.read("a" * 64)
    oversize = artifacts.finalize(
        CONTENT + b"x",
        metadata=metadata(),
        finalized_at=NOW,
    )
    client.get_error = S3ClientError(
        status_code=403,
        code="AccessDenied",
        detail=f"secret bucket {BUCKET}",
    )
    outage = artifacts.read(HASH)

    assert isinstance(invalid, Failure)
    assert invalid.error.code is ErrorCode.INVALID_INPUT
    assert isinstance(missing, Failure)
    assert missing.error.code is ErrorCode.NOT_FOUND
    assert isinstance(oversize, Failure)
    assert oversize.error.code is ErrorCode.INVALID_INPUT
    assert isinstance(outage, Failure)
    assert outage.error.code is ErrorCode.INTERNAL_ERROR
    assert BUCKET not in outage.error.message
    assert BUCKET not in str(outage.error.details)


def test_corrupt_or_oversize_stored_manifest_fails_closed() -> None:
    client = FakeS3Client()
    artifacts = store(client)
    unwrap(artifacts.finalize(CONTENT, metadata=metadata(), finalized_at=NOW))

    client.objects[manifest_key()].body = b"{"
    client.objects[manifest_key()].content_length = 1
    corrupt = artifacts.manifest(HASH)
    client.objects[manifest_key()].body = b"x" * 70_000
    client.objects[manifest_key()].content_length = 70_000
    client.objects[manifest_key()].checksum = base64.b64encode(
        hashlib.sha256(client.objects[manifest_key()].body).digest()
    ).decode()
    oversize = artifacts.manifest(HASH)

    assert isinstance(corrupt, Failure)
    assert corrupt.error.code is ErrorCode.CONFLICT
    assert isinstance(oversize, Failure)
    assert oversize.error.code is ErrorCode.CONFLICT


def test_presign_is_get_only_bounded_verified_and_secret_safe() -> None:
    client = FakeS3Client()
    artifacts = store(client)
    unwrap(artifacts.finalize(CONTENT, metadata=metadata(), finalized_at=NOW))
    issuer = S3ReadCapabilityIssuer(
        store=artifacts,
        client=client,
        clock=lambda: NOW,
        allowed_origin=ORIGIN,
        max_ttl_seconds=300,
    )
    assert isinstance(issuer, ArtifactReadCapabilityIssuerPort)

    capability = unwrap(
        issuer.issue_read_url(HASH, expires_at=NOW + timedelta(seconds=60))
    )

    assert capability.reveal_url().startswith(f"{ORIGIN}/{BUCKET}/{object_key()}?")
    assert capability.method == "GET"
    assert capability.expires_at == NOW + timedelta(seconds=60)
    assert capability.url == SecretStr(capability.reveal_url())
    assert capability.reveal_url() not in repr(capability)
    assert "url" not in capability.model_dump(mode="json")
    call = next(
        request
        for operation, request in client.calls
        if operation == "generate_presigned_url"
    )
    assert call == {
        "ClientMethod": "get_object",
        "Params": {
            "Bucket": BUCKET,
            "Key": object_key(),
        },
        "ExpiresIn": 60,
        "HttpMethod": "GET",
    }


def test_presign_rejects_missing_long_lived_or_wrong_origin_capability() -> None:
    client = FakeS3Client()
    artifacts = store(client)
    issuer = S3ReadCapabilityIssuer(
        store=artifacts,
        client=client,
        clock=lambda: NOW,
        allowed_origin=ORIGIN,
        max_ttl_seconds=300,
    )

    missing = issuer.issue_read_url(
        "a" * 64,
        expires_at=NOW + timedelta(seconds=60),
    )
    unwrap(artifacts.finalize(CONTENT, metadata=metadata(), finalized_at=NOW))
    long_lived = issuer.issue_read_url(
        HASH,
        expires_at=NOW + timedelta(seconds=301),
    )
    client.presigned_url = (
        f"https://attacker.example/{object_key()}"
        "?X-Amz-Expires=60&X-Amz-Signature=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    )
    wrong_origin = issuer.issue_read_url(
        HASH,
        expires_at=NOW + timedelta(seconds=60),
    )

    assert isinstance(missing, Failure)
    assert missing.error.code is ErrorCode.NOT_FOUND
    assert isinstance(long_lived, Failure)
    assert long_lived.error.code is ErrorCode.INVALID_INPUT
    assert isinstance(wrong_origin, Failure)
    assert wrong_origin.error.code is ErrorCode.INTERNAL_ERROR


@pytest.mark.parametrize(
    "kwargs",
    (
        {"bucket": "Bad_Bucket"},
        {"prefix": "../artifacts"},
        {"prefix": "/artifacts"},
        {"kms_key_id": " "},
        {"expected_bucket_owner": "owner"},
        {"max_size_bytes": -1},
    ),
)
def test_store_rejects_unbounded_or_ambiguous_configuration(
    kwargs: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "client": FakeS3Client(),
        "bucket": BUCKET,
        "prefix": PREFIX,
        "kms_key_id": KMS_KEY,
        "expected_bucket_owner": OWNER,
        "max_size_bytes": 1024,
    }
    values.update(kwargs)

    with pytest.raises(ValueError):
        S3ArtifactStore(**values)


def test_store_rejects_incomplete_encryption_or_retention_policy() -> None:
    client = FakeS3Client()

    with pytest.raises(ValueError):
        store(client, encryption=ArtifactEncryption.KMS, kms_key_id=None)
    with pytest.raises(ValueError):
        store(
            client,
            encryption=ArtifactEncryption.AES256,
            kms_key_id=KMS_KEY,
        )
    with pytest.raises(ValueError):
        store(
            client,
            retention={Sensitivity.INTERNAL: (ArtifactRetentionMode.GOVERNANCE, 30)},
        )


def test_manifest_envelope_rejects_non_finite_unknown_content() -> None:
    client = FakeS3Client()
    artifacts = store(client)
    unwrap(artifacts.finalize(CONTENT, metadata=metadata(), finalized_at=NOW))
    payload = json.loads(client.objects[manifest_key()].body)
    payload["unknown"] = format(Decimal("NaN"), "f")
    encoded = json.dumps(payload).encode()
    stored = client.objects[manifest_key()]
    stored.body = encoded
    stored.content_length = len(encoded)
    stored.checksum = base64.b64encode(hashlib.sha256(encoded).digest()).decode()

    result = artifacts.manifest(HASH)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
