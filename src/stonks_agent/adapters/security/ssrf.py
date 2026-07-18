"""Exact outbound endpoint policy with DNS pinning and SSRF denial."""

from __future__ import annotations

import ipaddress
import re
import socket
import ssl
import threading
from collections.abc import Iterable
from types import TracebackType
from typing import Literal, Protocol, Self, cast
from urllib.parse import SplitResult, urlsplit

import httpcore
import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

RuntimeEnvironment = Literal[
    "local",
    "development",
    "test",
    "staging",
    "production",
]

_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_MAX_ADDRESSES = 16
_DEFAULT_PORTS = {"http": 80, "https": 443}


class EndpointDenied(RuntimeError):
    """Public-safe fail-closed endpoint denial."""

    def __init__(self) -> None:
        super().__init__("Outbound endpoint is denied")


class HostResolver(Protocol):
    """Resolve every address considered by the outbound network path."""

    def resolve(self, host: str, port: int) -> tuple[str, ...]: ...


class PinnedEndpointGuard(Protocol):
    """Network transport contract for exact URL and connected-IP authority."""

    def authorize(self, value: str) -> None: ...

    def authorize_connected_address(self, value: str) -> None: ...

    def authorize_response(
        self,
        *,
        status_code: int,
        location: str | None,
    ) -> None: ...

    def connection_addresses(self, host: str, port: int) -> tuple[str, ...]: ...


class ExactEndpoint(BaseModel):
    """One normalized scheme/host/port/path allowlist entry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scheme: Literal["http", "https"]
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65_535)
    path: str = Field(min_length=1, max_length=1024)
    environment: RuntimeEnvironment = "production"

    @model_validator(mode="after")
    def validate_exact_shape(self) -> Self:
        if self.environment in {"staging", "production"} and self.scheme != "https":
            raise ValueError("deployed outbound endpoints require HTTPS")
        if not _valid_canonical_host(self.host):
            raise ValueError("outbound host must be canonical")
        if not _valid_exact_path(self.path):
            raise ValueError("outbound path must be exact")
        return self

    @classmethod
    def from_url(
        cls,
        value: str,
        *,
        environment: RuntimeEnvironment = "production",
    ) -> ExactEndpoint:
        parsed = _parsed_url(value)
        scheme = parsed.scheme
        if scheme not in _DEFAULT_PORTS:
            raise ValueError("outbound URL scheme is invalid")
        try:
            explicit_port = parsed.port
        except ValueError as error:
            raise ValueError("outbound URL port is invalid") from error
        port = _DEFAULT_PORTS[scheme] if explicit_port is None else explicit_port
        if (
            parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not _authority_is_exact(parsed, parsed.hostname, port)
        ):
            raise ValueError("outbound URL must be exact and credential-free")
        return cls(
            scheme=cast(Literal["http", "https"], scheme),
            host=parsed.hostname,
            port=port,
            path=parsed.path or "/",
            environment=environment,
        )

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        default_port = _DEFAULT_PORTS[self.scheme]
        authority = host if self.port == default_port else f"{host}:{self.port}"
        return f"{self.scheme}://{authority}{self.path}"


class SystemHostResolver:
    """Bounded system resolver used by production HTTP compositions."""

    __slots__ = ()

    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        try:
            records = socket.getaddrinfo(
                host,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except OSError as error:
            raise EndpointDenied from error
        addresses = {
            str(record[4][0])
            for record in records
            if record[4] and isinstance(record[4][0], str)
        }
        return tuple(sorted(addresses))


class OutboundEndpointGuard:
    """Authorize exact URLs and pin one stable, entirely public DNS answer."""

    __slots__ = ("_endpoint", "_lock", "_pinned_addresses", "_resolver")

    def __init__(
        self,
        endpoint: ExactEndpoint,
        *,
        resolver: HostResolver | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._resolver = resolver or SystemHostResolver()
        self._lock = threading.Lock()
        self._pinned_addresses: frozenset[str] = frozenset()

    @property
    def pinned_addresses(self) -> frozenset[str]:
        with self._lock:
            return self._pinned_addresses

    def authorize(self, value: str) -> None:
        if not _matches(self._endpoint, value):
            raise EndpointDenied
        try:
            resolved = self._resolver.resolve(
                self._endpoint.host,
                self._endpoint.port,
            )
        except EndpointDenied:
            raise
        except Exception as error:
            raise EndpointDenied from error
        addresses = _validated_addresses(resolved)
        with self._lock:
            if self._pinned_addresses and self._pinned_addresses != addresses:
                raise EndpointDenied
            self._pinned_addresses = addresses

    def authorize_connected_address(self, value: str) -> None:
        address = _public_address(value)
        with self._lock:
            if not self._pinned_addresses or address not in self._pinned_addresses:
                raise EndpointDenied

    def authorize_response(
        self,
        *,
        status_code: int,
        location: str | None,
    ) -> None:
        del location
        if 300 <= status_code < 400:
            raise EndpointDenied

    def connection_addresses(self, host: str, port: int) -> tuple[str, ...]:
        if host != self._endpoint.host or port != self._endpoint.port:
            raise EndpointDenied
        with self._lock:
            if not self._pinned_addresses:
                raise EndpointDenied
            return tuple(sorted(self._pinned_addresses))


class PinnedNetworkBackend(httpcore.NetworkBackend):
    """Replace the network stack's second DNS lookup with pinned IP connects."""

    __slots__ = ("_backend", "_guard")

    def __init__(
        self,
        guard: PinnedEndpointGuard,
        *,
        backend: httpcore.NetworkBackend | None = None,
    ) -> None:
        self._guard = guard
        self._backend = backend or httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        addresses = self._guard.connection_addresses(host, port)
        last_error: Exception | None = None
        for address in addresses:
            try:
                return self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                last_error = error
        if last_error is not None:
            raise last_error
        raise EndpointDenied

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        del path, timeout, socket_options
        raise EndpointDenied

    def sleep(self, seconds: float) -> None:
        self._backend.sleep(seconds)


class _ResponseStream(httpx.SyncByteStream):
    __slots__ = ("_stream",)

    def __init__(self, stream: Iterable[bytes]) -> None:
        self._stream = stream

    def __iter__(self):  # type: ignore[no-untyped-def]
        yield from self._stream

    def close(self) -> None:
        close = getattr(self._stream, "close", None)
        if close is not None:
            close()


class PinnedHTTPTransport(httpx.BaseTransport):
    """HTTPX transport whose TCP destination is the guard's public DNS pin."""

    __slots__ = ("_guard", "_pool")

    def __init__(
        self,
        guard: PinnedEndpointGuard,
        *,
        network_backend: httpcore.NetworkBackend | None = None,
    ) -> None:
        self._guard = guard
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl.create_default_context(),
            network_backend=PinnedNetworkBackend(
                guard,
                backend=network_backend,
            ),
            http1=True,
            http2=False,
            retries=0,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self._guard.authorize(str(request.url))
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=cast(Iterable[bytes], request.stream),
            extensions=request.extensions,
        )
        try:
            response = self._pool.handle_request(core_request)
        except httpcore.TimeoutException as error:
            raise httpx.TimeoutException(
                "Pinned outbound request timed out",
                request=request,
            ) from error
        except httpcore.NetworkError as error:
            raise httpx.NetworkError(
                "Pinned outbound network failed",
                request=request,
            ) from error
        except httpcore.ProtocolError as error:
            raise httpx.ProtocolError(
                "Pinned outbound protocol failed",
                request=request,
            ) from error
        try:
            self._guard.authorize_response(
                status_code=response.status,
                location=_header(response.headers, b"location"),
            )
        except EndpointDenied:
            _close_stream(response.stream)
            raise
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_ResponseStream(cast(Iterable[bytes], response.stream)),
            extensions=response.extensions,
        )

    def close(self) -> None:
        self._pool.close()

    def __enter__(self) -> Self:
        self._pool.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        self._pool.__exit__(exc_type, exc_value, traceback)


def _parsed_url(value: str) -> SplitResult:
    if (
        not value
        or value.strip() != value
        or "\\" in value
        or any(ord(character) < 0x21 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError("outbound URL contains unsafe characters")
    try:
        return urlsplit(value)
    except ValueError as error:
        raise ValueError("outbound URL is invalid") from error


def _matches(endpoint: ExactEndpoint, value: str) -> bool:
    try:
        parsed = _parsed_url(value)
        port = parsed.port or _DEFAULT_PORTS.get(parsed.scheme)
    except ValueError:
        return False
    return (
        parsed.scheme == endpoint.scheme
        and parsed.hostname == endpoint.host
        and port == endpoint.port
        and parsed.path == endpoint.path
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and _authority_is_exact(parsed, endpoint.host, endpoint.port)
    )


def _valid_canonical_host(value: str) -> bool:
    if value != value.lower() or value.endswith(".") or "%" in value:
        return False
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        labels = value.split(".")
        return len(labels) >= 2 and all(
            _HOST_LABEL.fullmatch(label) is not None for label in labels
        )
    return str(parsed) == value


def _authority_is_exact(
    parsed: SplitResult,
    host: str,
    port: int,
) -> bool:
    encoded_host = f"[{host}]" if ":" in host else host
    explicit = f"{encoded_host}:{port}"
    if port == _DEFAULT_PORTS.get(parsed.scheme):
        return parsed.netloc in {encoded_host, explicit}
    return parsed.netloc == explicit


def _valid_exact_path(value: str) -> bool:
    if (
        not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or "?" in value
        or "#" in value
        or "%" in value
        or any(ord(character) < 0x21 or ord(character) == 0x7F for character in value)
    ):
        return False
    return all(segment not in {".", ".."} for segment in value.split("/"))


def _validated_addresses(values: Iterable[str]) -> frozenset[str]:
    bounded = tuple(values)
    if not 1 <= len(bounded) <= _MAX_ADDRESSES:
        raise EndpointDenied
    addresses = frozenset(_public_address(value) for value in bounded)
    if not addresses or len(addresses) > _MAX_ADDRESSES:
        raise EndpointDenied
    return addresses


def _public_address(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise EndpointDenied from error
    if (
        not address.is_global
        or address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    ):
        raise EndpointDenied
    return str(address)


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    for key, value in headers:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _close_stream(stream: object) -> None:
    close = getattr(stream, "close", None)
    if close is not None:
        close()
