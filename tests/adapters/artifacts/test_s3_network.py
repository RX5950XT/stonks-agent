from __future__ import annotations

import pytest

from stonks_agent.adapters.artifacts.s3_network import (
    S3EndpointGuard,
    create_pinned_s3_http_client,
)
from stonks_agent.adapters.security.ssrf import EndpointDenied

ORIGIN = "https://objects.example"
BUCKET = "stonks-artifacts"
PREFIX = "prod/artifacts"
HASH = "a" * 64
OBJECT_URL = f"{ORIGIN}/{BUCKET}/{PREFIX}/objects/aa/{HASH}"


class Resolver:
    def __init__(self) -> None:
        self.addresses = ("93.184.216.34",)

    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        assert (host, port) == ("objects.example", 443)
        return self.addresses


def guard(resolver: Resolver | None = None) -> S3EndpointGuard:
    return S3EndpointGuard(
        endpoint_url=ORIGIN,
        bucket=BUCKET,
        prefix=PREFIX,
        resolver=resolver or Resolver(),
    )


@pytest.mark.parametrize(
    "url",
    (
        OBJECT_URL,
        (
            f"{ORIGIN}/{BUCKET}"
            "?max-keys=100&prefix=prod%2Fartifacts%2Fobjects%2F&versions="
        ),
        f"{ORIGIN}/{BUCKET}?versioning=",
        f"{ORIGIN}/{BUCKET}?object-lock=",
        (
            f"{ORIGIN}/{BUCKET}?key-marker=prod%2Fartifacts%2Fobjects%2Faa%2F"
            f"{HASH}&max-keys=100&prefix=prod%2Fartifacts%2Fobjects%2F"
            "&version-id-marker=version-1&versions="
        ),
        f"{OBJECT_URL}?retention=&versionId=version-1",
        f"{OBJECT_URL}?legal-hold=&versionId=version-1",
        f"{OBJECT_URL}?versionId=version-1",
    ),
)
def test_guard_authorizes_only_canonical_s3_runtime_urls(url: str) -> None:
    value = guard()

    value.authorize(url)

    assert value.pinned_addresses == frozenset({"93.184.216.34"})
    assert value.connection_addresses("objects.example", 443) == ("93.184.216.34",)


@pytest.mark.parametrize(
    "url",
    (
        "https://attacker.example/stonks-artifacts/prod/artifacts/objects/aa/" + HASH,
        f"{ORIGIN}/other-bucket/{PREFIX}/objects/aa/{HASH}",
        f"{ORIGIN}/{BUCKET}/other-prefix/objects/aa/{HASH}",
        f"{ORIGIN}/{BUCKET}/{PREFIX}/objects/../secret",
        f"{ORIGIN}/{BUCKET}/{PREFIX}/objects/%2e%2e/secret",
        f"{OBJECT_URL}?response-content-type=text%2Fhtml",
        f"{OBJECT_URL}?versionId=one&versionId=two",
        (
            f"{ORIGIN}/{BUCKET}"
            "?versions=&prefix=prod%2Fartifacts%2Fobjects%2F&max-keys=100"
        ),
        f"{OBJECT_URL}#fragment",
    ),
)
def test_guard_rejects_origin_scope_traversal_query_and_ambiguity(url: str) -> None:
    with pytest.raises(EndpointDenied):
        guard().authorize(url)


def test_guard_rejects_dns_rebind_connected_ip_and_redirect() -> None:
    resolver = Resolver()
    value = guard(resolver)
    value.authorize(OBJECT_URL)
    resolver.addresses = ("93.184.216.35",)

    with pytest.raises(EndpointDenied):
        value.authorize(OBJECT_URL)
    with pytest.raises(EndpointDenied):
        value.authorize_connected_address("93.184.216.99")
    with pytest.raises(EndpointDenied):
        value.authorize_response(
            status_code=307,
            location="http://169.254.169.254/latest/meta-data",
        )


def test_production_factory_rejects_http_and_unsafe_scope() -> None:
    with pytest.raises(ValueError):
        create_pinned_s3_http_client(
            endpoint_url="http://objects.example",
            bucket=BUCKET,
            prefix=PREFIX,
            timeout_seconds=5,
        )
    with pytest.raises(ValueError):
        create_pinned_s3_http_client(
            endpoint_url=ORIGIN,
            bucket=BUCKET,
            prefix="../escape",
            timeout_seconds=5,
        )
