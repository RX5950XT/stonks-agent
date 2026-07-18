from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import SecretStr

from stonks_agent.adapters.artifacts.s3 import S3ArtifactStore, S3ClientError
from stonks_agent.adapters.artifacts.s3_http import SigV4S3Client
from stonks_agent.domain.artifact import ArtifactMetadata
from stonks_agent.domain.artifact_retention import (
    ArtifactEncryption,
    ArtifactRetentionMode,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    StructuredError,
    Success,
)
from stonks_agent.domain.s3_credentials import S3CredentialBundle
from stonks_contracts.evidence import Sensitivity

NOW = datetime(2026, 7, 18, 10, tzinfo=UTC)
CONTENT = b'{"symbol":"AAPL","close":"100.00"}'
HASH = hashlib.sha256(CONTENT).hexdigest()
ORIGIN = "https://objects.example"
BUCKET = "stonks-artifacts"
PREFIX = "prod/artifacts"
KMS_KEY = "alias/stonks-artifacts"
OWNER = "123456789012"


class Credentials:
    def __init__(self, *, available: bool = True, expires_in: int = 600) -> None:
        self.available = available
        self.expires_in = expires_in
        self.calls = 0

    def resolve(self):  # type: ignore[no-untyped-def]
        self.calls += 1
        if not self.available:
            return Failure(
                StructuredError(
                    code=ErrorCode.DATA_UNAVAILABLE,
                    message="Credential provider unavailable",
                )
            )
        return Success(
            S3CredentialBundle(
                access_key_id=SecretStr("AK" + "IA" + "A" * 16),
                secret_access_key=SecretStr("runtime-" + "secret-" + "value"),
                session_token=SecretStr("runtime-" + "session-" + "token"),
                issued_at=NOW,
                expires_at=NOW + timedelta(seconds=self.expires_in),
                source="test-workload",
                version="test-v1",
            )
        )


class ObjectService:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, httpx.Headers]] = {}
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.url.host == "objects.example"
        assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 ")
        assert "x-amz-security-token" in request.headers
        assert request.headers["accept-encoding"] == "identity"
        key = request.url.path
        if request.method == "PUT":
            if request.headers.get("if-none-match") == "*" and key in self.objects:
                return httpx.Response(
                    412,
                    text="<Error><Code>PreconditionFailed</Code></Error>",
                    request=request,
                )
            body = request.content
            assert (
                request.headers["x-amz-content-sha256"]
                == hashlib.sha256(body).hexdigest()
            )
            stored_headers = httpx.Headers(
                {
                    **{
                        name: value
                        for name, value in request.headers.items()
                        if name.startswith("x-amz-meta-")
                    },
                    "content-length": str(len(body)),
                    "content-type": request.headers["content-type"],
                    "x-amz-checksum-sha256": request.headers["x-amz-checksum-sha256"],
                    "x-amz-server-side-encryption": request.headers[
                        "x-amz-server-side-encryption"
                    ],
                    "x-amz-version-id": f"version-{len(self.objects) + 1}",
                }
            )
            for name in (
                "x-amz-server-side-encryption-aws-kms-key-id",
                "x-amz-object-lock-mode",
                "x-amz-object-lock-retain-until-date",
            ):
                if name in request.headers:
                    stored_headers[name] = request.headers[name]
            self.objects[key] = body, stored_headers
            return httpx.Response(
                200,
                headers={
                    "x-amz-checksum-sha256": stored_headers["x-amz-checksum-sha256"],
                    "x-amz-version-id": stored_headers["x-amz-version-id"],
                },
                request=request,
            )
        stored = self.objects.get(key)
        if request.method == "GET" and stored is not None:
            return httpx.Response(
                200,
                content=stored[0],
                headers=stored[1],
                request=request,
            )
        return httpx.Response(
            404,
            text="<Error><Code>NoSuchKey</Code></Error>",
            request=request,
        )


def metadata() -> ArtifactMetadata:
    return ArtifactMetadata(
        media_type="application/json",
        license_tag="Apache-2.0",
        sensitivity=Sensitivity.INTERNAL,
        source="test",
    )


def client(
    service: ObjectService,
    credentials: Credentials | None = None,
) -> SigV4S3Client:
    return SigV4S3Client(
        http_client=httpx.Client(
            transport=httpx.MockTransport(service),
            follow_redirects=False,
            trust_env=False,
        ),
        credentials=credentials or Credentials(),
        endpoint_url=ORIGIN,
        region="us-east-1",
        bucket=BUCKET,
        prefix=PREFIX,
        expected_bucket_owner=OWNER,
        timeout_seconds=5,
        clock=lambda: NOW,
    )


def test_real_sigv4_client_round_trips_s3_store_without_default_chain() -> None:
    service = ObjectService()
    signed = client(service)
    store = S3ArtifactStore(
        client=signed,
        bucket=BUCKET,
        prefix=PREFIX,
        kms_key_id=KMS_KEY,
        expected_bucket_owner=OWNER,
        max_size_bytes=1024,
    )

    finalized = store.finalize(CONTENT, metadata=metadata(), finalized_at=NOW)

    assert isinstance(finalized, Success)
    assert store.read(HASH) == Success(CONTENT)
    assert tuple(request.method for request in service.calls).count("PUT") == 2
    assert all(request.url.query == b"" for request in service.calls)


def test_sigv4_round_trip_preserves_aes256_object_lock_identity() -> None:
    service = ObjectService()
    signed = client(service)
    store = S3ArtifactStore(
        client=signed,
        bucket=BUCKET,
        prefix=PREFIX,
        kms_key_id=None,
        encryption=ArtifactEncryption.AES256,
        retention={
            sensitivity: (ArtifactRetentionMode.COMPLIANCE, 30)
            for sensitivity in Sensitivity
        },
        expected_bucket_owner=OWNER,
        max_size_bytes=1024,
    )

    finalized = store.finalize(CONTENT, metadata=metadata(), finalized_at=NOW)

    assert isinstance(finalized, Success)
    assert store.read(HASH) == Success(CONTENT)
    puts = [request for request in service.calls if request.method == "PUT"]
    assert len(puts) == 2
    assert all(
        request.headers["x-amz-server-side-encryption"] == "AES256"
        and request.headers["x-amz-object-lock-mode"] == "COMPLIANCE"
        and "x-amz-server-side-encryption-aws-kms-key-id" not in request.headers
        for request in puts
    )


def test_presign_is_exact_get_and_credential_expiry_bounded() -> None:
    service = ObjectService()
    credentials = Credentials(expires_in=120)
    signed = client(service, credentials)
    key = f"{PREFIX}/objects/{HASH[:2]}/{HASH}"

    url = signed.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": BUCKET, "Key": key},
        ExpiresIn=60,
        HttpMethod="GET",
    )

    parsed = httpx.URL(url)
    assert parsed.host == "objects.example"
    assert parsed.path == f"/{BUCKET}/{key}"
    assert b"X-Amz-Signature=" in parsed.query
    assert b"X-Amz-Expires=60" in parsed.query
    assert credentials.calls == 1

    expiring = Credentials(expires_in=30)
    denied = client(service, expiring)
    try:
        denied.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": BUCKET, "Key": key},
            ExpiresIn=60,
            HttpMethod="GET",
        )
    except RuntimeError as error:
        assert str(error) == "S3 operation failed"
    else:
        raise AssertionError("credential expiry must deny presign")


def test_credential_failure_denies_before_network_and_errors_are_public_safe() -> None:
    service = ObjectService()
    credentials = Credentials(available=False)
    signed = client(service, credentials)

    try:
        signed.get_object(Bucket=BUCKET, Key=f"{PREFIX}/objects/{'a' * 64}")
    except RuntimeError as error:
        assert str(error) == "S3 operation failed"
        assert "runtime-secret" not in repr(error)
    else:
        raise AssertionError("credential failure must be raised")
    assert service.calls == []


def test_http_redirect_and_out_of_scope_key_fail_closed() -> None:
    redirect_calls: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        redirect_calls.append(request)
        return httpx.Response(
            307,
            headers={"location": "http://169.254.169.254/latest/meta-data"},
            request=request,
        )

    signed = SigV4S3Client(
        http_client=httpx.Client(
            transport=httpx.MockTransport(redirect),
            follow_redirects=False,
            trust_env=False,
        ),
        credentials=Credentials(),
        endpoint_url=ORIGIN,
        region="us-east-1",
        bucket=BUCKET,
        prefix=PREFIX,
        expected_bucket_owner=OWNER,
        timeout_seconds=5,
        clock=lambda: NOW,
    )

    for key in ("../secret", "other-prefix/object"):
        try:
            signed.get_object(Bucket=BUCKET, Key=key)
        except (RuntimeError, ValueError):
            pass
        else:
            raise AssertionError("out-of-scope key must fail")
    assert redirect_calls == []

    try:
        signed.get_object(Bucket=BUCKET, Key=f"{PREFIX}/objects/{'a' * 64}")
    except RuntimeError as error:
        assert str(error) == "S3 operation failed"
    else:
        raise AssertionError("redirect must fail")
    assert len(redirect_calls) == 1


def test_version_lock_and_restore_control_plane_is_sigv4_signed_and_exact() -> None:
    calls: list[httpx.Request] = []
    key = f"{PREFIX}/objects/{HASH[:2]}/{HASH}"
    version = "version-1"

    def control(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 ")
        if request.url.params.get("versions") == "":
            return httpx.Response(
                200,
                content=(
                    '<ListVersionsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                    "<IsTruncated>false</IsTruncated><Version>"
                    f"<Key>{key}</Key><VersionId>{version}</VersionId>"
                    "<IsLatest>true</IsLatest>"
                    "<LastModified>2026-07-18T10:00:00Z</LastModified>"
                    "</Version></ListVersionsResult>"
                ),
                request=request,
            )
        if request.url.params.get("retention") == "":
            if request.method == "GET":
                return httpx.Response(
                    200,
                    content=(
                        '<Retention xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                        "<Mode>COMPLIANCE</Mode>"
                        "<RetainUntilDate>2027-07-18T10:00:00Z</RetainUntilDate>"
                        "</Retention>"
                    ),
                    request=request,
                )
            assert b"<Mode>COMPLIANCE</Mode>" in request.content
            assert "content-md5" in request.headers
            return httpx.Response(200, request=request)
        if request.url.params.get("legal-hold") == "":
            if request.method == "GET":
                return httpx.Response(
                    200,
                    content=(
                        '<LegalHold xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                        "<Status>OFF</Status></LegalHold>"
                    ),
                    request=request,
                )
            assert b"<Status>ON</Status>" in request.content
            return httpx.Response(200, request=request)
        assert request.method == "DELETE"
        return httpx.Response(204, request=request)

    signed = SigV4S3Client(
        http_client=httpx.Client(
            transport=httpx.MockTransport(control),
            follow_redirects=False,
            trust_env=False,
        ),
        credentials=Credentials(),
        endpoint_url=ORIGIN,
        region="us-east-1",
        bucket=BUCKET,
        prefix=PREFIX,
        expected_bucket_owner=OWNER,
        timeout_seconds=5,
        clock=lambda: NOW,
    )
    common = {
        "Bucket": BUCKET,
        "Key": key,
        "VersionId": version,
        "ExpectedBucketOwner": OWNER,
    }

    listed = signed.list_object_versions(
        Bucket=BUCKET,
        Prefix=f"{PREFIX}/objects/",
        MaxKeys=100,
        ExpectedBucketOwner=OWNER,
    )
    signed.list_object_versions(
        Bucket=BUCKET,
        Prefix=f"{PREFIX}/objects/",
        MaxKeys=100,
        ExpectedBucketOwner=OWNER,
        KeyMarker=key,
        VersionIdMarker=version,
    )
    retention = signed.get_object_retention(**common)
    signed.put_object_retention(
        **common,
        Retention={
            "Mode": "COMPLIANCE",
            "RetainUntilDate": NOW + timedelta(days=365),
        },
    )
    hold = signed.get_object_legal_hold(**common)
    signed.put_object_legal_hold(**common, LegalHold={"Status": "ON"})
    signed.delete_object(**common)

    assert listed["Versions"][0]["VersionId"] == version
    assert retention["Retention"]["Mode"] == "COMPLIANCE"
    assert hold == {"LegalHold": {"Status": "OFF"}}
    assert len(calls) == 7
    assert calls[1].url.params["key-marker"] == key
    assert calls[1].url.params["version-id-marker"] == version
    assert all(
        request.headers["x-amz-expected-bucket-owner"] == OWNER for request in calls
    )


def test_control_plane_rejects_governance_bypass_and_untrusted_version() -> None:
    signed = client(ObjectService())
    common = {
        "Bucket": BUCKET,
        "Key": f"{PREFIX}/objects/{HASH[:2]}/{HASH}",
        "VersionId": "version-1",
        "ExpectedBucketOwner": OWNER,
    }

    for payload in (
        {**common, "BypassGovernanceRetention": True},
        {**common, "VersionId": "bad\nversion"},
    ):
        try:
            signed.delete_object(**payload)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe version operation must be rejected")


def test_bucket_preflight_controls_are_signed_and_strictly_parsed() -> None:
    calls: list[httpx.Request] = []

    def controls(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 ")
        if request.url.params.get("versioning") == "":
            content = (
                '<VersioningConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                "<Status>Enabled</Status></VersioningConfiguration>"
            )
        else:
            content = (
                '<ObjectLockConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                "<ObjectLockEnabled>Enabled</ObjectLockEnabled>"
                "</ObjectLockConfiguration>"
            )
        return httpx.Response(200, content=content, request=request)

    signed = SigV4S3Client(
        http_client=httpx.Client(
            transport=httpx.MockTransport(controls),
            follow_redirects=False,
            trust_env=False,
        ),
        credentials=Credentials(),
        endpoint_url=ORIGIN,
        region="us-east-1",
        bucket=BUCKET,
        prefix=PREFIX,
        expected_bucket_owner=OWNER,
        timeout_seconds=5,
        clock=lambda: NOW,
    )

    assert signed.get_bucket_versioning(
        Bucket=BUCKET,
        ExpectedBucketOwner=OWNER,
    ) == {"Status": "Enabled"}
    assert signed.get_object_lock_configuration(
        Bucket=BUCKET,
        ExpectedBucketOwner=OWNER,
    ) == {"ObjectLockEnabled": "Enabled"}
    assert len(calls) == 2


def test_insecure_endpoint_is_only_available_for_explicit_loopback_test() -> None:
    values = {
        "http_client": httpx.Client(
            transport=httpx.MockTransport(ObjectService()),
            follow_redirects=False,
            trust_env=False,
        ),
        "credentials": Credentials(),
        "region": "us-east-1",
        "bucket": BUCKET,
        "prefix": PREFIX,
        "expected_bucket_owner": OWNER,
        "timeout_seconds": 5,
        "clock": lambda: NOW,
    }

    for endpoint, allow in (
        ("http://127.0.0.1:18333", False),
        ("http://objects.example", True),
        ("https://objects.example", True),
    ):
        try:
            SigV4S3Client(
                **values,
                endpoint_url=endpoint,
                allow_insecure_loopback=allow,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe endpoint mode must be rejected")

    value = SigV4S3Client(
        **values,
        endpoint_url="http://127.0.0.1:18333",
        allow_insecure_loopback=True,
    )
    assert isinstance(value, SigV4S3Client)


def test_sigv4_debug_logging_cannot_emit_credential_material(caplog) -> None:  # type: ignore[no-untyped-def]
    service = ObjectService()
    signed = client(service)
    caplog.set_level(logging.DEBUG, logger="botocore.auth")
    key = f"{PREFIX}/objects/{HASH[:2]}/{HASH}"

    signed.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": BUCKET, "Key": key},
        ExpiresIn=60,
        HttpMethod="GET",
    )

    rendered = caplog.text
    for forbidden in (
        "AKIA",
        "runtime-secret-value",
        "runtime-session-token",
        "CanonicalRequest",
        "StringToSign",
    ):
        assert forbidden not in rendered


def test_unsafe_or_oversize_error_documents_are_not_parsed() -> None:
    responses = (
        b"<!DOCTYPE Error [<!ENTITY x 'provider-secret'>]>"
        b"<Error><Code>&x;</Code></Error>",
        b"<Error><Code>" + b"A" * 9_000 + b"</Code></Error>",
    )
    for body in responses:

        def unsafe(request: httpx.Request, payload: bytes = body) -> httpx.Response:
            return httpx.Response(500, content=payload, request=request)

        signed = SigV4S3Client(
            http_client=httpx.Client(
                transport=httpx.MockTransport(unsafe),
                follow_redirects=False,
                trust_env=False,
            ),
            credentials=Credentials(),
            endpoint_url=ORIGIN,
            region="us-east-1",
            bucket=BUCKET,
            prefix=PREFIX,
            expected_bucket_owner=OWNER,
            timeout_seconds=5,
            clock=lambda: NOW,
        )

        try:
            signed.get_object(Bucket=BUCKET, Key=f"{PREFIX}/objects/{'a' * 64}")
        except S3ClientError as error:
            assert error.code == "HTTP500"
            assert "provider-secret" not in repr(error)
        else:
            raise AssertionError("unsafe provider error must fail")
