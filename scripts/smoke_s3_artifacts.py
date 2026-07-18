#!/usr/bin/env python3
"""Exercise the real SigV4/httpx path against the pinned S3-compatible image."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import SecretStr

from stonks_agent.adapters.artifacts.s3 import S3ArtifactStore
from stonks_agent.adapters.artifacts.s3_http import SigV4S3Client
from stonks_agent.domain.artifact import ArtifactMetadata
from stonks_agent.domain.artifact_retention import ArtifactEncryption
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.s3_credentials import S3CredentialBundle
from stonks_contracts.evidence import Sensitivity

_CONTENT = b'{"schema":"stonks/s3-smoke/1","value":"verified"}'
_BUCKET = "stonks-artifacts"
_PREFIX = "smoke/artifacts"
_OWNER = "000000000000"


class _StaticCredentials:
    def __init__(self, access_key: str, secret_key: str) -> None:
        self._access_key = SecretStr(access_key)
        self._secret_key = SecretStr(secret_key)

    def resolve(self) -> Success[S3CredentialBundle]:
        current = datetime.now(UTC)
        return Success(
            S3CredentialBundle(
                access_key_id=self._access_key,
                secret_access_key=self._secret_key,
                issued_at=current,
                expires_at=current + timedelta(minutes=10),
                source="static-test",
                version="seaweedfs-4.34",
            )
        )


def main() -> int:
    endpoint = _required("STONKS_S3_TEST_ENDPOINT")
    credentials = _StaticCredentials(
        _required("STONKS_S3_TEST_ACCESS_KEY"),
        _required("STONKS_S3_TEST_SECRET_KEY"),
    )
    with httpx.Client(follow_redirects=False, trust_env=False) as http:
        client = _client(http, credentials, endpoint)
        store = _store(client)
        content_hash = _exercise_store(store)
        _exercise_presign(client, content_hash, endpoint)
    print(
        json.dumps(
            {
                "content_hash": content_hash,
                "conditional_finalize": "verified",
                "hash_round_trip": "verified",
                "presigned_get": "verified",
                "runtime": "seaweedfs-4.34",
            },
            sort_keys=True,
        )
    )
    return 0


def _client(
    http: httpx.Client,
    credentials: _StaticCredentials,
    endpoint: str,
) -> SigV4S3Client:
    return SigV4S3Client(
        http_client=http,
        credentials=credentials,
        endpoint_url=endpoint,
        region="us-east-1",
        bucket=_BUCKET,
        prefix=_PREFIX,
        expected_bucket_owner=_OWNER,
        timeout_seconds=5,
        clock=lambda: datetime.now(UTC),
        allow_insecure_loopback=True,
    )


def _store(client: SigV4S3Client) -> S3ArtifactStore:
    return S3ArtifactStore(
        client=client,
        bucket=_BUCKET,
        prefix=_PREFIX,
        kms_key_id=None,
        encryption=ArtifactEncryption.AES256,
        expected_bucket_owner=_OWNER,
        max_size_bytes=1_024,
    )


def _exercise_store(store: S3ArtifactStore) -> str:
    metadata = ArtifactMetadata(
        media_type="application/json",
        license_tag="Apache-2.0",
        sensitivity=Sensitivity.INTERNAL,
        source="s3-compatible-smoke",
    )
    first = store.finalize(_CONTENT, metadata=metadata, finalized_at=datetime.now(UTC))
    if not isinstance(first, Success):
        raise RuntimeError("S3 smoke finalize failed")
    retried = store.finalize(
        _CONTENT, metadata=metadata, finalized_at=datetime.now(UTC)
    )
    read = store.read(first.value.content_hash)
    conflict = store.finalize(
        _CONTENT,
        metadata=metadata.model_copy(update={"license_tag": "MIT"}),
        finalized_at=datetime.now(UTC),
    )
    if retried != first or read != Success(_CONTENT):
        raise RuntimeError("S3 smoke round trip failed")
    if (
        not isinstance(conflict, Failure)
        or conflict.error.code is not ErrorCode.CONFLICT
    ):
        raise RuntimeError("S3 smoke conflict control failed")
    return first.value.content_hash


def _exercise_presign(
    client: SigV4S3Client,
    content_hash: str,
    endpoint: str,
) -> None:
    key = f"{_PREFIX}/objects/{content_hash[:2]}/{content_hash}"
    url = client.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": _BUCKET, "Key": key},
        ExpiresIn=60,
        HttpMethod="GET",
    )
    if not isinstance(url, str) or not url.startswith(f"{endpoint}/{_BUCKET}/"):
        raise RuntimeError("S3 smoke capability scope failed")
    with httpx.Client(follow_redirects=False, trust_env=False, timeout=5) as reader:
        response = reader.get(url, headers={"accept-encoding": "identity"})
    if response.status_code != 200 or response.content != _CONTENT:
        raise RuntimeError("S3 smoke presigned read failed")


def _required(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value or value.strip() != value:
        raise RuntimeError("S3 smoke configuration is invalid")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
