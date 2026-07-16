from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from stonks_agent.adapters.auth.oidc import HTTPSJWKSetProvider, OIDCSettings
from stonks_agent.domain.errors import ErrorCode, Failure, Success

ISSUER = "https://identity.example.test"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
FIRST_KID = "signing-key-1"
SECOND_KID = "signing-key-2"
FIRST_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SECOND_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

Handler = Callable[[httpx.Request], httpx.Response]


class ManualClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def settings(**overrides: object) -> OIDCSettings:
    values: dict[str, object] = {
        "issuer": ISSUER,
        "audience": "stonks-core-api",
        "jwks_url": JWKS_URL,
        "allowed_algorithms": ("RS256",),
        "allowed_client_ids": ("stonks-web",),
        "max_token_lifetime_seconds": 900,
        "clock_skew_seconds": 30,
        "jwks_cache_seconds": 30,
        "jwks_min_refresh_seconds": 10,
        "jwks_timeout_seconds": 1.5,
    }
    values.update(overrides)
    return OIDCSettings.model_validate(values)


def public_jwk(private_key: object, *, kid: str) -> dict[str, object]:
    key = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    return {**key, "kid": kid, "alg": "RS256", "use": "sig"}


def jwks_response(
    request: httpx.Request,
    keys: list[dict[str, object]],
    *,
    status_code: int = 200,
    content_type: str = "application/jwk-set+json",
    extra_headers: dict[str, str] | None = None,
) -> httpx.Response:
    headers = {"content-type": content_type, **(extra_headers or {})}
    return httpx.Response(
        status_code,
        headers=headers,
        json={"keys": keys},
        request=request,
    )


@contextmanager
def provider(
    handler: Handler,
    *,
    clock: ManualClock | None = None,
) -> Iterator[HTTPSJWKSetProvider]:
    selected_clock = clock or ManualClock()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        yield HTTPSJWKSetProvider(settings(), client, clock=selected_clock)


def assert_unauthorized(result: object) -> None:
    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.UNAUTHORIZED
    assert result.error.message == "Authentication failed"
    assert result.error.details == {}


def test_fetches_once_then_serves_an_unexpired_cached_key() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET"
        assert str(request.url) == JWKS_URL
        assert request.headers["accept"] == (
            "application/jwk-set+json, application/json"
        )
        assert request.headers["accept-encoding"] == "identity"
        return jwks_response(
            request,
            [public_jwk(FIRST_PRIVATE_KEY, kid=FIRST_KID)],
        )

    with provider(handler) as keys:
        first = keys.signing_key(FIRST_KID, "RS256")
        cached = keys.signing_key(FIRST_KID, "RS256")

    assert isinstance(first, Success)
    assert isinstance(cached, Success)
    assert len(calls) == 1


def test_unknown_rotated_kid_gets_one_immediate_bounded_refresh() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        selected = (
            public_jwk(FIRST_PRIVATE_KEY, kid=FIRST_KID)
            if calls == 1
            else public_jwk(SECOND_PRIVATE_KEY, kid=SECOND_KID)
        )
        return jwks_response(request, [selected])

    with provider(handler) as keys:
        initial = keys.signing_key(FIRST_KID, "RS256")
        rotated = keys.signing_key(SECOND_KID, "RS256")
        removed = keys.signing_key(FIRST_KID, "RS256")

    assert isinstance(initial, Success)
    assert isinstance(rotated, Success)
    assert_unauthorized(removed)
    assert calls == 2


def test_repeated_unknown_kids_cannot_force_unbounded_refreshes() -> None:
    clock = ManualClock()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return jwks_response(
            request,
            [public_jwk(FIRST_PRIVATE_KEY, kid=FIRST_KID)],
        )

    with provider(handler, clock=clock) as keys:
        assert isinstance(keys.signing_key(FIRST_KID, "RS256"), Success)
        assert_unauthorized(keys.signing_key("missing-1", "RS256"))
        assert_unauthorized(keys.signing_key("missing-2", "RS256"))
        assert calls == 2

        clock.advance(10)
        assert_unauthorized(keys.signing_key("missing-3", "RS256"))

    assert calls == 3


def test_cold_cache_outage_is_refresh_rate_limited() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("identity provider unavailable", request=request)

    with provider(handler) as keys:
        assert_unauthorized(keys.signing_key(FIRST_KID, "RS256"))
        assert_unauthorized(keys.signing_key(FIRST_KID, "RS256"))

    assert calls == 1


def test_expired_cache_fails_closed_and_outage_retry_is_bounded() -> None:
    clock = ManualClock()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return jwks_response(
                request,
                [public_jwk(FIRST_PRIVATE_KEY, kid=FIRST_KID)],
            )
        raise httpx.ReadTimeout("JWKS request timed out", request=request)

    with provider(handler, clock=clock) as keys:
        assert isinstance(keys.signing_key(FIRST_KID, "RS256"), Success)
        clock.advance(31)
        assert_unauthorized(keys.signing_key(FIRST_KID, "RS256"))
        assert_unauthorized(keys.signing_key(FIRST_KID, "RS256"))

    assert calls == 2


def test_failed_rotation_does_not_destroy_an_unexpired_known_key() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return jwks_response(
                request,
                [public_jwk(FIRST_PRIVATE_KEY, kid=FIRST_KID)],
            )
        raise httpx.ReadTimeout("JWKS request timed out", request=request)

    with provider(handler) as keys:
        assert isinstance(keys.signing_key(FIRST_KID, "RS256"), Success)
        assert_unauthorized(keys.signing_key(SECOND_KID, "RS256"))
        retained = keys.signing_key(FIRST_KID, "RS256")

    assert isinstance(retained, Success)
    assert calls == 2


def test_duplicate_kids_reject_the_entire_set_and_bound_retries() -> None:
    calls = 0
    duplicate = public_jwk(FIRST_PRIVATE_KEY, kid=FIRST_KID)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return jwks_response(request, [duplicate, duplicate])

    with provider(handler) as keys:
        assert_unauthorized(keys.signing_key(FIRST_KID, "RS256"))
        assert_unauthorized(keys.signing_key(FIRST_KID, "RS256"))

    assert calls == 1


@pytest.mark.parametrize(
    ("status_code", "content_type", "extra_headers"),
    [
        (302, "application/jwk-set+json", {"location": "https://attacker.test"}),
        (503, "application/jwk-set+json", None),
        (200, "text/html", None),
        (200, "application/jwk-set+json", {"content-encoding": "gzip"}),
    ],
)
def test_http_status_media_type_redirect_and_encoding_fail_closed(
    status_code: int,
    content_type: str,
    extra_headers: dict[str, str] | None,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return jwks_response(
            request,
            [public_jwk(FIRST_PRIVATE_KEY, kid=FIRST_KID)],
            status_code=status_code,
            content_type=content_type,
            extra_headers=extra_headers,
        )

    with provider(handler) as keys:
        assert_unauthorized(keys.signing_key(FIRST_KID, "RS256"))

    assert calls == 1


@pytest.mark.parametrize(
    "body",
    [
        b"x" * 65_537,
        b'{"keys":',
        json.dumps({"keys": [], "issuer": ISSUER}).encode(),
    ],
    ids=("oversize", "invalid-json", "extra-top-level-field"),
)
def test_oversize_invalid_json_and_extra_top_level_fields_fail_closed(
    body: bytes,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/jwk-set+json"},
            content=body,
            request=request,
        )

    with provider(handler) as keys:
        assert_unauthorized(keys.signing_key(FIRST_KID, "RS256"))


def test_key_algorithm_mismatch_fails_without_discarding_the_cache() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return jwks_response(
            request,
            [public_jwk(FIRST_PRIVATE_KEY, kid=FIRST_KID)],
        )

    with provider(handler) as keys:
        assert_unauthorized(keys.signing_key(FIRST_KID, "ES256"))
        matching = keys.signing_key(FIRST_KID, "RS256")

    assert isinstance(matching, Success)
    assert calls == 1
