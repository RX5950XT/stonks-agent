"""SigV4 S3 transport using only injected credentials and an httpx client."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import logging
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Final
from urllib.parse import quote, urlencode, urlsplit

import httpx
from botocore.auth import S3SigV4Auth, S3SigV4QueryAuth  # type: ignore[import-untyped]
from botocore.awsrequest import AWSRequest  # type: ignore[import-untyped]
from botocore.credentials import Credentials  # type: ignore[import-untyped]

from stonks_agent.adapters.artifacts import s3_xml
from stonks_agent.adapters.artifacts.s3 import S3ClientError
from stonks_agent.domain.errors import Failure
from stonks_agent.domain.s3_credentials import (
    S3CredentialBundle,
    S3CredentialProvider,
)

_REGION_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_BUCKET_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_PREFIX_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,511}$")
_OWNER_PATTERN: Final = re.compile(r"^[0-9]{12}$")
_KEY_PATTERN: Final = re.compile(r"^[a-z0-9][A-Za-z0-9._/-]{0,1023}$")
_SAFE_KEY = "/-_.~"
_MAX_ERROR_BODY = 8_192
_PRESIGN_SAFETY = timedelta(seconds=15)


class _DenySigV4DetailLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.WARNING


logging.getLogger("botocore.auth").addFilter(_DenySigV4DetailLogs())


class _HTTPStreamingBody:
    """Small boto-style streaming body wrapper over one httpx response."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self._iterator = response.iter_bytes(chunk_size=65_536)
        self._buffer = bytearray()
        self._eof = False

    def read(self, amount: int = -1) -> bytes:
        if amount == 0:
            return b""
        if amount < -1:
            raise ValueError("read amount is invalid")
        if amount == -1:
            self._fill(None)
            result = bytes(self._buffer)
            self._buffer.clear()
            return result
        self._fill(amount)
        result = bytes(self._buffer[:amount])
        del self._buffer[:amount]
        return result

    def close(self) -> None:
        self._response.close()

    def _fill(self, amount: int | None) -> None:
        while not self._eof and (amount is None or len(self._buffer) < amount):
            try:
                self._buffer.extend(next(self._iterator))
            except StopIteration:
                self._eof = True


class SigV4S3Client:
    """Narrow S3 client with exact origin, bucket and prefix authority."""

    def __init__(
        self,
        *,
        http_client: httpx.Client,
        credentials: S3CredentialProvider,
        endpoint_url: str,
        region: str,
        bucket: str,
        prefix: str,
        expected_bucket_owner: str,
        timeout_seconds: float,
        clock: Callable[[], datetime],
        allow_insecure_loopback: bool = False,
    ) -> None:
        self._origin = _validate_origin(
            endpoint_url,
            allow_insecure_loopback=allow_insecure_loopback,
        )
        self._region = _validate_value(region, _REGION_PATTERN, "region")
        self._bucket = _validate_value(bucket, _BUCKET_PATTERN, "bucket")
        self._prefix = _validate_prefix(prefix)
        self._owner = _validate_value(
            expected_bucket_owner, _OWNER_PATTERN, "bucket owner"
        )
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0.1 <= timeout_seconds <= 60
        ):
            raise ValueError("S3 timeout is invalid")
        self._http = http_client
        self._credentials = credentials
        self._timeout = float(timeout_seconds)
        self._clock = clock

    def put_object(self, **kwargs: object) -> object:
        request = self._validate_common(kwargs, operation="put")
        body = kwargs.get("Body")
        metadata = kwargs.get("Metadata")
        if not isinstance(body, bytes) or not isinstance(metadata, Mapping):
            raise ValueError("S3 put payload is invalid")
        headers = _put_headers(kwargs, metadata, body)
        response = self._send("PUT", request.key, headers=headers, body=body)
        try:
            return {
                "ChecksumSHA256": response.headers.get("x-amz-checksum-sha256"),
                "VersionId": response.headers.get("x-amz-version-id"),
            }
        finally:
            response.close()

    def get_object(self, **kwargs: object) -> object:
        request = self._validate_common(kwargs, operation="get")
        headers = {
            "accept-encoding": "identity",
            "x-amz-expected-bucket-owner": self._owner,
            "x-amz-checksum-mode": "ENABLED",
        }
        response = self._send("GET", request.key, headers=headers, body=None)
        return _get_response(response)

    def generate_presigned_url(self, **kwargs: object) -> object:
        method, key, ttl = self._validate_presign(kwargs)
        bundle = self._resolve_credentials(ttl=ttl)
        aws = AWSRequest(method=method, url=self._url(key), headers={})
        S3SigV4QueryAuth(
            _botocore_credentials(bundle),
            "s3",
            self._region,
            expires=ttl,
        ).add_auth(aws)
        prepared = aws.prepare()
        if not isinstance(prepared.url, str):
            raise RuntimeError("S3 operation failed")
        return prepared.url

    def list_object_versions(self, **kwargs: object) -> object:
        prefix, maximum, markers = self._validate_list(kwargs)
        query = {"versions": "", "prefix": prefix, "max-keys": str(maximum)}
        query.update(markers)
        response = self._send_control("GET", None, query=query)
        return _parse_document(s3_xml.parse_version_listing, response)

    def get_bucket_versioning(self, **kwargs: object) -> object:
        self._validate_bucket_request(kwargs)
        response = self._send_control("GET", None, query={"versioning": ""})
        return _parse_document(s3_xml.parse_versioning, response)

    def get_object_lock_configuration(self, **kwargs: object) -> object:
        self._validate_bucket_request(kwargs)
        response = self._send_control("GET", None, query={"object-lock": ""})
        return _parse_document(
            s3_xml.parse_object_lock_configuration,
            response,
        )

    def get_object_retention(self, **kwargs: object) -> object:
        key, version = self._validate_version_request(kwargs)
        response = self._send_control(
            "GET",
            key,
            query={"retention": "", "versionId": version},
        )
        return {"Retention": _parse_document(s3_xml.parse_retention, response)}

    def put_object_retention(self, **kwargs: object) -> object:
        key, version = self._validate_version_request(
            kwargs,
            extra={"Retention"},
        )
        retention = kwargs.get("Retention")
        if not isinstance(retention, Mapping):
            raise ValueError("S3 retention is invalid")
        body = s3_xml.retention_xml(retention)
        self._send_control(
            "PUT",
            key,
            query={"retention": "", "versionId": version},
            body=body,
        )
        return {}

    def get_object_legal_hold(self, **kwargs: object) -> object:
        key, version = self._validate_version_request(kwargs)
        response = self._send_control(
            "GET",
            key,
            query={"legal-hold": "", "versionId": version},
        )
        return {"LegalHold": _parse_document(s3_xml.parse_legal_hold, response)}

    def put_object_legal_hold(self, **kwargs: object) -> object:
        key, version = self._validate_version_request(
            kwargs,
            extra={"LegalHold"},
        )
        hold = kwargs.get("LegalHold")
        if not isinstance(hold, Mapping) or set(hold) != {"Status"}:
            raise ValueError("S3 legal hold is invalid")
        status = hold.get("Status")
        if status != "ON":
            raise ValueError("S3 legal hold can only be enabled")
        self._send_control(
            "PUT",
            key,
            query={"legal-hold": "", "versionId": version},
            body=s3_xml.legal_hold_xml(),
        )
        return {}

    def delete_object(self, **kwargs: object) -> object:
        key, version = self._validate_version_request(kwargs)
        self._send_control("DELETE", key, query={"versionId": version})
        return {}

    def _send(
        self,
        method: str,
        key: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> httpx.Response:
        return self._send_url(
            method,
            self._url(key),
            headers=headers,
            body=body,
        )

    def _send_control(
        self,
        method: str,
        key: str | None,
        *,
        query: Mapping[str, str],
        body: bytes | None = None,
    ) -> bytes:
        url = self._control_url(key, query)
        headers = {
            "accept-encoding": "identity",
            "x-amz-expected-bucket-owner": self._owner,
        }
        if body is not None:
            headers.update(_xml_headers(body))
        response = self._send_url(method, url, headers=headers, body=body)
        try:
            return _read_control_body(response)
        finally:
            response.close()

    def _send_url(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> httpx.Response:
        bundle = self._resolve_credentials()
        signed = _sign_headers(
            method=method,
            url=url,
            headers=headers,
            body=body,
            credentials=bundle,
            region=self._region,
        )
        request = self._http.build_request(
            method,
            url,
            headers=signed,
            content=body,
            timeout=self._timeout,
        )
        return self._send_request(request)

    def _send_request(self, request: httpx.Request) -> httpx.Response:
        try:
            response = self._http.send(
                request,
                stream=True,
                follow_redirects=False,
            )
        except httpx.HTTPError as error:
            raise S3ClientError(status_code=None, code="TransportError") from error
        if 300 <= response.status_code < 400:
            response.close()
            raise S3ClientError(status_code=response.status_code, code="Redirect")
        if response.is_error:
            response_error = _response_error(response)
            response.close()
            raise response_error
        return response

    def _resolve_credentials(self, *, ttl: int = 0) -> S3CredentialBundle:
        resolved = self._credentials.resolve()
        if isinstance(resolved, Failure):
            raise RuntimeError("S3 operation failed")
        current = self._clock()
        if (
            not isinstance(current, datetime)
            or current.tzinfo is None
            or current.utcoffset() is None
            or resolved.value.expires_at
            <= current.astimezone(UTC) + timedelta(seconds=ttl) + _PRESIGN_SAFETY
        ):
            raise RuntimeError("S3 operation failed")
        return resolved.value

    def _validate_common(
        self,
        kwargs: Mapping[str, object],
        *,
        operation: str,
    ) -> _ValidatedRequest:
        allowed = {
            "put": {
                "Bucket",
                "Key",
                "ExpectedBucketOwner",
                "Body",
                "ContentType",
                "Metadata",
                "ChecksumSHA256",
                "ServerSideEncryption",
                "SSEKMSKeyId",
                "BucketKeyEnabled",
                "IfNoneMatch",
                "ObjectLockMode",
                "ObjectLockRetainUntilDate",
            },
            "get": {"Bucket", "Key", "ExpectedBucketOwner", "ChecksumMode"},
        }[operation]
        required = {"Bucket", "Key"}
        if not required.issubset(kwargs) or not set(kwargs).issubset(allowed):
            raise ValueError("S3 operation fields are invalid")
        if kwargs.get("Bucket") != self._bucket:
            raise ValueError("S3 bucket is outside configured authority")
        if kwargs.get("ExpectedBucketOwner", self._owner) != self._owner:
            raise ValueError("S3 bucket owner is invalid")
        key = kwargs.get("Key")
        if not isinstance(key, str) or not self._valid_key(key):
            raise ValueError("S3 key is outside configured authority")
        if operation == "get" and kwargs.get("ChecksumMode", "ENABLED") != "ENABLED":
            raise ValueError("S3 checksum mode is invalid")
        return _ValidatedRequest(key=key)

    def _validate_presign(self, kwargs: Mapping[str, object]) -> tuple[str, str, int]:
        if set(kwargs) != {"ClientMethod", "Params", "ExpiresIn", "HttpMethod"}:
            raise ValueError("S3 presign fields are invalid")
        if kwargs.get("ClientMethod") != "get_object":
            raise ValueError("S3 presign operation is invalid")
        if kwargs.get("HttpMethod") != "GET":
            raise ValueError("S3 presign method is invalid")
        params = kwargs.get("Params")
        if not isinstance(params, Mapping) or set(params) != {"Bucket", "Key"}:
            raise ValueError("S3 presign target is invalid")
        if params.get("Bucket") != self._bucket:
            raise ValueError("S3 bucket is outside configured authority")
        key = params.get("Key")
        ttl = kwargs.get("ExpiresIn")
        if not isinstance(key, str) or not self._valid_key(key):
            raise ValueError("S3 key is outside configured authority")
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not 1 <= ttl <= 900:
            raise ValueError("S3 presign TTL is invalid")
        return "GET", key, ttl

    def _validate_list(
        self,
        kwargs: Mapping[str, object],
    ) -> tuple[str, int, dict[str, str]]:
        required = {"Bucket", "Prefix", "ExpectedBucketOwner", "MaxKeys"}
        optional = {"KeyMarker", "VersionIdMarker"}
        if not required.issubset(kwargs) or not set(kwargs).issubset(
            required | optional
        ):
            raise ValueError("S3 version listing fields are invalid")
        self._validate_bucket_owner(kwargs)
        prefix, maximum = kwargs.get("Prefix"), kwargs.get("MaxKeys")
        if (
            not isinstance(prefix, str)
            or not self._valid_list_prefix(prefix)
            or isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or not 1 <= maximum <= 1_000
        ):
            raise ValueError("S3 version listing scope is invalid")
        markers = self._list_markers(kwargs)
        return prefix, maximum, markers

    def _list_markers(self, kwargs: Mapping[str, object]) -> dict[str, str]:
        present = {"KeyMarker", "VersionIdMarker"} & set(kwargs)
        if not present:
            return {}
        key = kwargs.get("KeyMarker")
        version = kwargs.get("VersionIdMarker")
        if (
            present != {"KeyMarker", "VersionIdMarker"}
            or not isinstance(key, str)
            or not self._valid_key(key)
            or not isinstance(version, str)
            or not _valid_version_id(version)
        ):
            raise ValueError("S3 version markers are invalid")
        return {"key-marker": key, "version-id-marker": version}

    def _valid_list_prefix(self, value: str) -> bool:
        normalized = value.removesuffix("/")
        return (
            value.endswith("/")
            and self._valid_key(normalized)
            and normalized
            in {
                f"{self._prefix}/objects",
                f"{self._prefix}/manifests",
            }
        ) or self._valid_key(value)

    def _validate_version_request(
        self,
        kwargs: Mapping[str, object],
        *,
        extra: set[str] | None = None,
    ) -> tuple[str, str]:
        expected = {
            "Bucket",
            "Key",
            "VersionId",
            "ExpectedBucketOwner",
            *(extra or set()),
        }
        if set(kwargs) != expected:
            raise ValueError("S3 version operation fields are invalid")
        self._validate_bucket_owner(kwargs)
        key, version = kwargs.get("Key"), kwargs.get("VersionId")
        if (
            not isinstance(key, str)
            or not self._valid_key(key)
            or not isinstance(version, str)
            or not _valid_version_id(version)
        ):
            raise ValueError("S3 version target is invalid")
        return key, version

    def _validate_bucket_owner(self, kwargs: Mapping[str, object]) -> None:
        if (
            kwargs.get("Bucket") != self._bucket
            or kwargs.get("ExpectedBucketOwner") != self._owner
        ):
            raise ValueError("S3 bucket authority is invalid")

    def _validate_bucket_request(self, kwargs: Mapping[str, object]) -> None:
        if set(kwargs) != {"Bucket", "ExpectedBucketOwner"}:
            raise ValueError("S3 bucket operation fields are invalid")
        self._validate_bucket_owner(kwargs)

    def _valid_key(self, key: str) -> bool:
        return (
            key.isascii()
            and _KEY_PATTERN.fullmatch(key) is not None
            and key.startswith(f"{self._prefix}/")
            and "//" not in key
            and all(part not in {"", ".", ".."} for part in key.split("/"))
        )

    def _url(self, key: str) -> str:
        return f"{self._origin}/{self._bucket}/{quote(key, safe=_SAFE_KEY)}"

    def _control_url(self, key: str | None, query: Mapping[str, str]) -> str:
        base = f"{self._origin}/{self._bucket}" if key is None else self._url(key)
        encoded = urlencode(tuple(sorted(query.items())), quote_via=quote)
        return f"{base}?{encoded}"


class _ValidatedRequest:
    def __init__(self, *, key: str) -> None:
        self.key = key


def _put_headers(
    kwargs: Mapping[str, object],
    metadata: Mapping[object, object],
    body: bytes,
) -> dict[str, str]:
    encryption = kwargs.get("ServerSideEncryption")
    kms = encryption == "aws:kms"
    aes = encryption == "AES256"
    if not (kms or aes) or kwargs.get("IfNoneMatch") != "*":
        raise ValueError("S3 put security controls are invalid")
    fields = ("ContentType", "ChecksumSHA256")
    if any(not isinstance(kwargs.get(name), str) for name in fields):
        raise ValueError("S3 put headers are invalid")
    if kms != (
        isinstance(kwargs.get("SSEKMSKeyId"), str)
        and kwargs.get("BucketKeyEnabled") is True
    ):
        raise ValueError("S3 KMS headers are invalid")
    if aes and ({"SSEKMSKeyId", "BucketKeyEnabled"} & set(kwargs)):
        raise ValueError("S3 AES headers are invalid")
    headers = {
        "accept-encoding": "identity",
        "content-type": str(kwargs["ContentType"]),
        "if-none-match": "*",
        "x-amz-checksum-sha256": str(kwargs["ChecksumSHA256"]),
        "x-amz-content-sha256": _hex_digest(body),
        "x-amz-server-side-encryption": str(encryption),
    }
    if kms:
        headers.update(
            {
                "x-amz-server-side-encryption-aws-kms-key-id": str(
                    kwargs["SSEKMSKeyId"]
                ),
                "x-amz-server-side-encryption-bucket-key-enabled": "true",
            }
        )
    headers.update(_object_lock_headers(kwargs))
    for raw_name, raw_value in metadata.items():
        if (
            not isinstance(raw_name, str)
            or not isinstance(raw_value, str)
            or re.fullmatch(r"[a-z0-9-]{1,64}", raw_name) is None
            or not _valid_header(raw_value)
        ):
            raise ValueError("S3 metadata is invalid")
        headers[f"x-amz-meta-{raw_name}"] = raw_value
    return headers


def _object_lock_headers(kwargs: Mapping[str, object]) -> dict[str, str]:
    fields = {"ObjectLockMode", "ObjectLockRetainUntilDate"}
    present = fields & set(kwargs)
    if not present:
        return {}
    if present != fields:
        raise ValueError("S3 object lock headers are incomplete")
    mode = kwargs.get("ObjectLockMode")
    until = s3_xml.normalize_datetime(kwargs.get("ObjectLockRetainUntilDate"))
    if mode not in {"GOVERNANCE", "COMPLIANCE"} or until is None:
        raise ValueError("S3 object lock headers are invalid")
    return {
        "x-amz-object-lock-mode": str(mode),
        "x-amz-object-lock-retain-until-date": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _sign_headers(
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    credentials: S3CredentialBundle,
    region: str,
) -> dict[str, str]:
    aws = AWSRequest(method=method, url=url, headers=dict(headers), data=body)
    S3SigV4Auth(
        _botocore_credentials(credentials),
        "s3",
        region,
    ).add_auth(aws)
    return {name: value for name, value in aws.headers.items()}


def _botocore_credentials(bundle: S3CredentialBundle) -> Credentials:
    access, secret, token = bundle.reveal()
    return Credentials(access, secret, token)


def _get_response(response: httpx.Response) -> dict[str, object]:
    metadata = {
        name.removeprefix("x-amz-meta-"): value
        for name, value in response.headers.items()
        if name.startswith("x-amz-meta-")
    }
    content_length = response.headers.get("content-length")
    if content_length is None or not content_length.isascii():
        response.close()
        raise S3ClientError(status_code=200, code="InvalidResponse")
    try:
        size = int(content_length)
    except ValueError as error:
        response.close()
        raise S3ClientError(status_code=200, code="InvalidResponse") from error
    return {
        "Body": _HTTPStreamingBody(response),
        "ContentLength": size,
        "ContentType": response.headers.get("content-type"),
        "ContentEncoding": response.headers.get("content-encoding"),
        "Metadata": metadata,
        "ChecksumSHA256": response.headers.get("x-amz-checksum-sha256"),
        "ServerSideEncryption": response.headers.get("x-amz-server-side-encryption"),
        "SSEKMSKeyId": response.headers.get(
            "x-amz-server-side-encryption-aws-kms-key-id"
        ),
        "VersionId": response.headers.get("x-amz-version-id"),
        "ObjectLockMode": response.headers.get("x-amz-object-lock-mode"),
        "ObjectLockRetainUntilDate": s3_xml.parse_datetime(
            response.headers.get("x-amz-object-lock-retain-until-date")
        ),
    }


def _response_error(response: httpx.Response) -> S3ClientError:
    payload = bytearray()
    try:
        for chunk in response.iter_bytes(chunk_size=4_096):
            if len(payload) + len(chunk) > _MAX_ERROR_BODY:
                payload.clear()
                break
            payload.extend(chunk)
    except httpx.HTTPError:
        payload.clear()
    code = _xml_error_code(bytes(payload)) or f"HTTP{response.status_code}"
    return S3ClientError(status_code=response.status_code, code=code)


def _xml_error_code(body: bytes) -> str | None:
    return s3_xml.parse_error_code(body[: _MAX_ERROR_BODY + 1])


def _xml_headers(body: bytes) -> dict[str, str]:
    digest = hashlib.md5(body, usedforsecurity=False).digest()
    return {
        "content-md5": base64.b64encode(digest).decode("ascii"),
        "content-type": "application/xml",
        "x-amz-content-sha256": _hex_digest(body),
    }


def _read_control_body(response: httpx.Response) -> bytes:
    payload = bytearray()
    try:
        for chunk in response.iter_bytes(chunk_size=65_536):
            if len(payload) + len(chunk) > s3_xml.MAX_CONTROL_BODY:
                raise S3ClientError(status_code=200, code="InvalidResponse")
            payload.extend(chunk)
    except httpx.HTTPError as error:
        raise S3ClientError(status_code=200, code="InvalidResponse") from error
    return bytes(payload)


def _parse_document[T](parser: Callable[[bytes], T], body: bytes) -> T:
    try:
        return parser(body)
    except s3_xml.S3DocumentError as error:
        raise S3ClientError(status_code=200, code="InvalidResponse") from error


def _hex_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _valid_header(value: str) -> bool:
    return (
        value.isascii()
        and 1 <= len(value) <= 2_048
        and all(character not in "\r\n\x00" for character in value)
    )


def _valid_version_id(value: str) -> bool:
    return (
        value.isascii()
        and 1 <= len(value) <= 1_024
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )


def _validate_origin(
    value: str,
    *,
    allow_insecure_loopback: bool,
) -> str:
    if not isinstance(value, str) or not value.isascii():
        raise ValueError("S3 endpoint is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("S3 endpoint is invalid")
    insecure = parsed.scheme == "http"
    if allow_insecure_loopback != insecure or (
        insecure and not _loopback(parsed.hostname)
    ):
        raise ValueError("S3 insecure endpoint is invalid")
    return value.rstrip("/")


def _loopback(host: str | None) -> bool:
    if host is None:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_value(value: str, pattern: re.Pattern[str], name: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"S3 {name} is invalid")
    return value


def _validate_prefix(value: str) -> str:
    value = _validate_value(value, _PREFIX_PATTERN, "prefix")
    if (
        value.endswith("/")
        or "//" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("S3 prefix is invalid")
    return value
