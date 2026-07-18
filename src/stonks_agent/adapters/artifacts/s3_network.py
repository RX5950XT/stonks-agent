"""Production S3 HTTP composition with exact scope and public DNS pinning."""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx

from stonks_agent.adapters.security.ssrf import (
    EndpointDenied,
    ExactEndpoint,
    HostResolver,
    OutboundEndpointGuard,
    PinnedHTTPTransport,
    RuntimeEnvironment,
)

_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_PREFIX_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,511}$")
_VERSION_PATTERN = re.compile(r"^[\x21-\x7e]{1,1024}$")


class S3EndpointGuard:
    """Authorize the dynamic paths of one prevalidated S3 bucket and prefix."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        prefix: str,
        environment: RuntimeEnvironment = "production",
        resolver: HostResolver | None = None,
    ) -> None:
        endpoint = ExactEndpoint.from_url(
            f"{endpoint_url.rstrip('/')}/",
            environment=environment,
        )
        if (
            _BUCKET_PATTERN.fullmatch(bucket) is None
            or _PREFIX_PATTERN.fullmatch(prefix) is None
            or prefix.endswith("/")
            or "//" in prefix
            or any(part in {"", ".", ".."} for part in prefix.split("/"))
        ):
            raise ValueError("S3 endpoint scope is invalid")
        self._origin = endpoint.url.rstrip("/")
        self._bucket_path = f"/{bucket}"
        self._scope_path = f"{self._bucket_path}/{prefix}/"
        self._list_prefixes = (f"{prefix}/objects/", f"{prefix}/manifests/")
        self._base = OutboundEndpointGuard(endpoint, resolver=resolver)

    @property
    def pinned_addresses(self) -> frozenset[str]:
        return self._base.pinned_addresses

    def authorize(self, value: str) -> None:
        if not self._matches(value):
            raise EndpointDenied
        self._base.authorize(f"{self._origin}/")

    def authorize_connected_address(self, value: str) -> None:
        self._base.authorize_connected_address(value)

    def authorize_response(
        self,
        *,
        status_code: int,
        location: str | None,
    ) -> None:
        self._base.authorize_response(status_code=status_code, location=location)

    def connection_addresses(self, host: str, port: int) -> tuple[str, ...]:
        return self._base.connection_addresses(host, port)

    def _matches(self, value: str) -> bool:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return False
        if (
            not value.isascii()
            or "\\" in value
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or f"{parsed.scheme}://{parsed.netloc}" != self._origin
            or any(ord(character) < 0x21 for character in value)
        ):
            return False
        if parsed.path == self._bucket_path:
            return self._valid_list_query(parsed.query)
        if not parsed.path.startswith(self._scope_path) or "%" in parsed.path:
            return False
        return _safe_path(parsed.path) and self._valid_object_query(parsed.query)

    def _valid_list_query(self, raw: str) -> bool:
        query = _query(raw)
        if query is None:
            return False
        if set(query) in ({"versioning"}, {"object-lock"}):
            return next(iter(query.values())) == "" and _canonical_query(query) == raw
        base = {"versions", "prefix", "max-keys"}
        markers = {"key-marker", "version-id-marker"}
        if set(query) not in (base, base | markers):
            return False
        maximum = query["max-keys"]
        prefix = query["prefix"]
        return (
            query["versions"] == ""
            and maximum.isdecimal()
            and 1 <= int(maximum) <= 1_000
            and any(prefix.startswith(allowed) for allowed in self._list_prefixes)
            and _valid_markers(query)
            and _canonical_query(query) == raw
        )

    def _valid_object_query(self, raw: str) -> bool:
        if not raw:
            return True
        query = _query(raw)
        if query is None:
            return False
        fields = set(query)
        valid_shape = fields in (
            {"retention", "versionId"},
            {"legal-hold", "versionId"},
            {"versionId"},
        )
        blank_controls = all(
            query[name] == "" for name in fields & {"retention", "legal-hold"}
        )
        return (
            valid_shape
            and blank_controls
            and _VERSION_PATTERN.fullmatch(query["versionId"]) is not None
            and _canonical_query(query) == raw
        )


def create_pinned_s3_http_client(
    *,
    endpoint_url: str,
    bucket: str,
    prefix: str,
    timeout_seconds: float,
    resolver: HostResolver | None = None,
) -> httpx.Client:
    """Create the only production client composition; ambient proxies are off."""

    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0.1 <= timeout_seconds <= 60
    ):
        raise ValueError("S3 HTTP timeout is invalid")
    guard = S3EndpointGuard(
        endpoint_url=endpoint_url,
        bucket=bucket,
        prefix=prefix,
        environment="production",
        resolver=resolver,
    )
    return httpx.Client(
        transport=PinnedHTTPTransport(guard),
        timeout=float(timeout_seconds),
        follow_redirects=False,
        trust_env=False,
        headers={"accept-encoding": "identity"},
    )


def _query(raw: str) -> dict[str, str] | None:
    try:
        parsed = parse_qs(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    if not parsed or any(len(values) != 1 for values in parsed.values()):
        return None
    return {name: values[0] for name, values in parsed.items()}


def _canonical_query(values: Mapping[str, str]) -> str:
    return urlencode(tuple(sorted(values.items())), quote_via=quote)


def _safe_path(value: str) -> bool:
    return (
        "//" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/")[1:])
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )


def _valid_markers(query: Mapping[str, str]) -> bool:
    values = tuple(
        query[name] for name in ("key-marker", "version-id-marker") if name in query
    )
    return not values or all(
        _VERSION_PATTERN.fullmatch(value) is not None for value in values
    )
