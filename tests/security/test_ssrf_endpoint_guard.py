from __future__ import annotations

import ssl
from collections.abc import Iterable
from typing import Any

import httpcore
import httpx
import pytest

from stonks_agent.adapters.security.ssrf import (
    EndpointDenied,
    ExactEndpoint,
    OutboundEndpointGuard,
    PinnedHTTPTransport,
    PinnedNetworkBackend,
)


class SequenceResolver:
    def __init__(self, answers: Iterable[tuple[str, ...]]) -> None:
        self._answers = iter(answers)
        self.calls: list[tuple[str, int]] = []

    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        self.calls.append((host, port))
        return next(self._answers)


def endpoint(**overrides: object) -> ExactEndpoint:
    values: dict[str, object] = {
        "scheme": "https",
        "host": "api.example.test",
        "port": 443,
        "path": "/v1/reports",
        "environment": "production",
    }
    values.update(overrides)
    return ExactEndpoint.model_validate(values)


def test_exact_endpoint_authorizes_and_pins_public_resolution() -> None:
    resolver = SequenceResolver(
        [
            ("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"),
            ("2606:2800:220:1:248:1893:25c8:1946", "93.184.216.34"),
        ]
    )
    guard = OutboundEndpointGuard(endpoint(), resolver=resolver)

    guard.authorize("https://api.example.test/v1/reports")
    guard.authorize("https://api.example.test:443/v1/reports")

    assert resolver.calls == [
        ("api.example.test", 443),
        ("api.example.test", 443),
    ]
    assert guard.pinned_addresses == frozenset(
        {"93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"}
    )


@pytest.mark.parametrize(
    ("url", "answers"),
    (
        ("http://127.0.0.1:11434/v1/chat/completions", ("127.0.0.1",)),
        ("http://localhost:11434/v1/chat/completions", ("127.0.0.1", "::1")),
    ),
)
def test_exact_local_model_endpoint_allows_only_pinned_loopback(
    url: str,
    answers: tuple[str, ...],
) -> None:
    endpoint = ExactEndpoint.from_url(url, environment="local")
    guard = OutboundEndpointGuard(
        endpoint,
        resolver=SequenceResolver([answers]),
    )

    guard.authorize(url)
    for address in answers:
        guard.authorize_connected_address(address)

    assert guard.pinned_addresses == frozenset(answers)


@pytest.mark.parametrize(
    "answer",
    ["10.0.0.1", "169.254.169.254", "192.168.1.1", "127.0.0.1", "::1"],
)
def test_public_model_hostname_cannot_resolve_to_private_or_loopback(
    answer: str,
) -> None:
    url = "https://models.example.com/v1/chat/completions"
    guard = OutboundEndpointGuard(
        ExactEndpoint.from_url(url, environment="local"),
        resolver=SequenceResolver([(answer,)]),
    )

    with pytest.raises(EndpointDenied, match="Outbound endpoint is denied"):
        guard.authorize(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.test:0/v1/reports",
        "https://api.example.test:/v1/reports",
        "https://api.example.test:0443/v1/reports",
        "https://api.example.test:+443/v1/reports",
        "https://api.example.test:-1/v1/reports",
        "https://api.example.test:65536/v1/reports",
        "https://API.example.test/v1/reports",
    ],
)
def test_from_url_rejects_invalid_or_noncanonical_port_authority(url: str) -> None:
    with pytest.raises(ValueError):
        ExactEndpoint.from_url(url, environment="production")


def test_from_url_normalizes_an_explicit_default_port_without_changing_rule() -> None:
    implicit = ExactEndpoint.from_url(
        "https://api.example.test/v1/reports",
        environment="production",
    )
    explicit = ExactEndpoint.from_url(
        "https://api.example.test:443/v1/reports",
        environment="production",
    )

    assert explicit == implicit
    assert explicit.port == 443
    assert explicit.url == "https://api.example.test/v1/reports"


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.test/v1/reports",
        "https://api.example.test:444/v1/reports",
        "https://other.example.test/v1/reports",
        "https://api.example.test/v1/other",
        "https://user@api.example.test/v1/reports",
        "https://api.example.test/v1/reports#fragment",
        "https://api.example.test/v1/reports?next=https://169.254.169.254",
        "https://api.example.test/v1/reports/",
        "https://api.example.test\\@169.254.169.254/v1/reports",
        "https://api.example.test:0/v1/reports",
        "https://api.example.test:/v1/reports",
        "https://api.example.test:0443/v1/reports",
    ],
)
def test_non_exact_or_ambiguous_endpoint_is_denied_before_resolution(url: str) -> None:
    resolver = SequenceResolver([("93.184.216.34",)])
    guard = OutboundEndpointGuard(endpoint(), resolver=resolver)

    with pytest.raises(EndpointDenied, match="Outbound endpoint is denied"):
        guard.authorize(url)

    assert resolver.calls == []


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.0.1",
        "169.254.169.254",
        "fe80::1",
        "224.0.0.1",
        "ff02::1",
        "0.0.0.0",
        "::",
        "192.0.2.1",
        "2001:db8::1",
        "240.0.0.1",
    ],
)
def test_non_public_resolution_is_denied(address: str) -> None:
    guard = OutboundEndpointGuard(
        endpoint(),
        resolver=SequenceResolver([(address,)]),
    )

    with pytest.raises(EndpointDenied, match="Outbound endpoint is denied"):
        guard.authorize("https://api.example.test/v1/reports")

    assert guard.pinned_addresses == frozenset()


def test_empty_invalid_or_oversized_resolution_is_denied() -> None:
    for answer in (
        (),
        ("not-an-ip",),
        tuple(f"8.8.8.{index}" for index in range(17)),
    ):
        guard = OutboundEndpointGuard(
            endpoint(),
            resolver=SequenceResolver([answer]),
        )
        with pytest.raises(EndpointDenied, match="Outbound endpoint is denied"):
            guard.authorize("https://api.example.test/v1/reports")


def test_dns_rebinding_and_connected_peer_mismatch_are_denied() -> None:
    guard = OutboundEndpointGuard(
        endpoint(),
        resolver=SequenceResolver(
            [
                ("93.184.216.34",),
                ("8.8.8.8",),
            ]
        ),
    )
    guard.authorize("https://api.example.test/v1/reports")

    with pytest.raises(EndpointDenied, match="Outbound endpoint is denied"):
        guard.authorize("https://api.example.test/v1/reports")
    with pytest.raises(EndpointDenied, match="Outbound endpoint is denied"):
        guard.authorize_connected_address("8.8.8.8")

    guard.authorize_connected_address("93.184.216.34")


def test_network_backend_connects_to_pinned_ip_without_second_dns_lookup() -> None:
    class Stream(httpcore.NetworkStream):
        def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
            del max_bytes, timeout
            return b""

        def write(self, buffer: bytes, timeout: float | None = None) -> None:
            del buffer, timeout

        def close(self) -> None:
            return None

        def start_tls(
            self,
            ssl_context: ssl.SSLContext,
            server_hostname: str | None = None,
            timeout: float | None = None,
        ) -> httpcore.NetworkStream:
            del ssl_context, server_hostname, timeout
            return self

        def get_extra_info(self, info: str) -> Any:
            del info
            return None

    class Backend(httpcore.NetworkBackend):
        def __init__(self) -> None:
            self.connects: list[tuple[str, int]] = []
            self.stream = Stream()

        def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
        ) -> httpcore.NetworkStream:
            del timeout, local_address, socket_options
            self.connects.append((host, port))
            return self.stream

        def connect_unix_socket(
            self,
            path: str,
            timeout: float | None = None,
            socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
        ) -> httpcore.NetworkStream:
            raise AssertionError((path, timeout, socket_options))

        def sleep(self, seconds: float) -> None:
            raise AssertionError(seconds)

    guard = OutboundEndpointGuard(
        endpoint(),
        resolver=SequenceResolver([("93.184.216.34",)]),
    )
    guard.authorize("https://api.example.test/v1/reports")
    backend = Backend()

    stream = PinnedNetworkBackend(guard, backend=backend).connect_tcp(
        "api.example.test",
        443,
    )

    assert stream is backend.stream
    assert backend.connects == [("93.184.216.34", 443)]


def test_pinned_transport_closes_redirect_response_before_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Stream:
        def __init__(self) -> None:
            self.closed = False

        def __iter__(self):
            yield b""

        def close(self) -> None:
            self.closed = True

    class Pool:
        def __init__(self) -> None:
            self.stream = Stream()

        def handle_request(self, request: httpcore.Request) -> httpcore.Response:
            assert request.url.host == b"api.example.test"
            return httpcore.Response(
                307,
                headers=[(b"location", b"http://169.254.169.254/latest/meta-data")],
                content=self.stream,
            )

        def close(self) -> None:
            return None

        def __enter__(self) -> Pool:
            return self

        def __exit__(self, *args: object) -> None:
            del args

    pool = Pool()
    monkeypatch.setattr(httpcore, "ConnectionPool", lambda **_: pool)
    guard = OutboundEndpointGuard(
        endpoint(),
        resolver=SequenceResolver([("93.184.216.34",)]),
    )
    transport = PinnedHTTPTransport(guard)

    with pytest.raises(EndpointDenied, match="Outbound endpoint is denied"):
        transport.handle_request(
            httpx.Request("GET", "https://api.example.test/v1/reports")
        )

    assert pool.stream.closed


@pytest.mark.parametrize("status", [300, 301, 302, 303, 307, 308, 399])
def test_all_redirects_are_denied_even_when_location_looks_allowlisted(
    status: int,
) -> None:
    guard = OutboundEndpointGuard(
        endpoint(),
        resolver=SequenceResolver([("93.184.216.34",)]),
    )

    with pytest.raises(EndpointDenied, match="Outbound endpoint is denied"):
        guard.authorize_response(
            status_code=status,
            location="https://api.example.test/v1/reports",
        )


def test_production_and_staging_default_to_https() -> None:
    for environment in ("staging", "production"):
        with pytest.raises(ValueError, match="HTTPS"):
            endpoint(scheme="http", port=80, environment=environment)


def test_rule_rejects_noncanonical_host_and_path() -> None:
    for overrides in (
        {"host": "API.example.test"},
        {"host": "api.example.test."},
        {"host": "api_example.test"},
        {"path": "//v1/reports"},
        {"path": "/v1/reports?query=1"},
        {"path": "/v1/reports#fragment"},
        {"path": "/v1/../reports"},
    ):
        with pytest.raises(ValueError):
            endpoint(**overrides)
