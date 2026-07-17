from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from stonks_agent.adapters.auth.local_token import DenyAllAuthenticator
from stonks_agent.domain.auth import LocalPrincipal, Role
from stonks_agent.domain.errors import Failure, Success
from stonks_agent.domain.rate_limit import RateLimitDecision
from stonks_agent.entrypoints.api.api_security import (
    ApiSecurityOptions,
    ApiSecurityPolicy,
    install_api_security,
)
from stonks_agent.entrypoints.api.dependencies.auth import (
    ReadPrincipal,
    install_authentication,
)
from stonks_agent.entrypoints.api.rate_limits import InMemoryRateLimitStore
from stonks_agent.ports.authentication import AuthenticationRequest


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 17, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class BearerSubjectAuthenticator:
    def __init__(self) -> None:
        self.calls = 0

    def authenticate(
        self,
        request: AuthenticationRequest,
    ) -> Success[LocalPrincipal]:
        self.calls += 1
        subject = (request.authorization or "Bearer anonymous").removeprefix("Bearer ")
        return Success(
            LocalPrincipal(
                subject=f"user:{subject}",
                roles=frozenset({Role.VIEWER}),
            )
        )


class TokenMappingAuthenticator:
    def __init__(self, subjects: dict[str, str]) -> None:
        self._subjects = subjects
        self.calls = 0

    def authenticate(
        self,
        request: AuthenticationRequest,
    ) -> Success[LocalPrincipal] | Failure:
        self.calls += 1
        token = (request.authorization or "").removeprefix("Bearer ")
        subject = self._subjects.get(token)
        if subject is None:
            return DenyAllAuthenticator().authenticate(request)
        return Success(
            LocalPrincipal(
                subject=subject,
                roles=frozenset({Role.VIEWER}),
            )
        )


class FalseyRateLimitStore(InMemoryRateLimitStore):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def __bool__(self) -> bool:
        return False

    def consume(
        self,
        key: str,
        *,
        now: datetime,
        limit: int,
        window_seconds: int,
    ) -> RateLimitDecision:
        self.calls += 1
        return super().consume(
            key,
            now=now,
            limit=limit,
            window_seconds=window_seconds,
        )


def _secured_app(
    authenticator: object,
    *,
    clock: Callable[[], datetime],
    limit: int = 1,
    edge_limit: int = 100,
    origins: tuple[str, ...] = (),
) -> FastAPI:
    app = FastAPI()
    install_api_security(
        app,
        max_request_bytes=8,
        options=ApiSecurityOptions(
            policy=ApiSecurityPolicy(
                allowed_cors_origins=origins,
                rate_limit_requests=limit,
                rate_limit_window_seconds=60,
                direct_peer_edge_requests=edge_limit,
            ),
            rate_limit_store=InMemoryRateLimitStore(),
            clock=clock,
        ),
    )
    install_authentication(app, authenticator)  # type: ignore[arg-type]

    @app.get("/protected")
    def protected(principal: ReadPrincipal) -> dict[str, str]:
        return {"subject": principal.subject}

    @app.post("/echo")
    async def echo() -> dict[str, bool]:
        return {"ok": True}

    return app


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "https://example.com/",
        "https://user@example.com",
        "https://example.com/path",
        "https://example.com?query=1",
        "https://EXAMPLE.com",
        "https://example.com.",
        "https://example.com:443",
        "http://localhost:80",
        "ftp://example.com",
    ],
)
def test_cors_policy_requires_canonical_exact_origins(origin: str) -> None:
    with pytest.raises(ValidationError):
        ApiSecurityPolicy(allowed_cors_origins=(origin,))


def test_cors_policy_accepts_canonical_https_and_bracketed_local_ipv6() -> None:
    policy = ApiSecurityPolicy(
        allowed_cors_origins=(
            "https://example.com",
            "https://example.com:8443",
            "http://[::1]:8080",
        )
    )

    assert policy.allowed_cors_origins == (
        "https://example.com",
        "https://example.com:8443",
        "http://[::1]:8080",
    )


def test_installation_reuses_only_identical_runtime_dependencies() -> None:
    app = FastAPI()
    clock = MutableClock()
    store = FalseyRateLimitStore()
    options = ApiSecurityOptions(rate_limit_store=store, clock=clock)

    install_api_security(app, max_request_bytes=8, options=options)
    install_api_security(app, max_request_bytes=8, options=options)
    install_authentication(app, DenyAllAuthenticator())

    @app.get("/public")
    def public() -> dict[str, bool]:
        return {"ok": True}

    assert TestClient(app).get("/public").status_code == 200
    assert store.calls == 2
    with pytest.raises(ValueError):
        install_api_security(
            app,
            max_request_bytes=8,
            options=ApiSecurityOptions(
                rate_limit_store=store,
                clock=MutableClock(),
            ),
        )


def test_middleware_order_bounds_body_before_auth_and_wraps_errors() -> None:
    app = _secured_app(
        BearerSubjectAuthenticator(),
        clock=MutableClock(),
        origins=("https://console.example.com",),
    )

    assert [item.cls.__name__ for item in app.user_middleware] == [
        "_SecurityHeadersMiddleware",
        "_ExceptionBoundaryMiddleware",
        "ApiAdmissionRateLimitMiddleware",
        "ExactCORSMiddleware",
        "ForwardedHeaderRejectMiddleware",
        "_CookieAuthenticationMiddleware",
        "RequestBodyLimitMiddleware",
        "ApiPrincipalRateLimitMiddleware",
    ]


def test_exact_cors_and_security_headers_cover_success_and_rejection() -> None:
    clock = MutableClock()
    client = TestClient(
        _secured_app(
            BearerSubjectAuthenticator(),
            clock=clock,
            limit=10,
            origins=("https://console.example.com",),
        )
    )

    allowed = client.get(
        "/protected",
        headers={
            "Authorization": "Bearer alice",
            "Origin": "https://console.example.com",
        },
    )
    suffix = client.get(
        "/protected",
        headers={
            "Authorization": "Bearer bob",
            "Origin": "https://console.example.com.evil.invalid",
        },
    )
    oversized = client.post(
        "/echo",
        headers={
            "Content-Length": "9",
            "Origin": "https://console.example.com",
        },
    )

    assert allowed.headers["access-control-allow-origin"] == (
        "https://console.example.com"
    )
    assert "access-control-allow-origin" not in suffix.headers
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "payload_too_large"
    assert oversized.headers["access-control-allow-origin"] == (
        "https://console.example.com"
    )
    for response in (allowed, suffix, oversized):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["content-security-policy"] == (
            "default-src 'none'; base-uri 'none'; form-action 'self'; "
            "frame-ancestors 'none'; object-src 'none'; script-src 'none'; "
            "style-src 'self'"
        )
        assert response.headers["permissions-policy"] == (
            "camera=(), geolocation=(), microphone=()"
        )


def test_oversize_request_is_rejected_before_authenticator_work() -> None:
    clock = MutableClock()
    authenticator = BearerSubjectAuthenticator()
    client = TestClient(
        _secured_app(
            authenticator,
            clock=clock,
            limit=10,
        )
    )

    response = client.post(
        "/echo",
        headers={
            "Authorization": "Bearer expensive-oidc-credential",
            "Content-Length": "9",
        },
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    assert authenticator.calls == 0


def test_rate_limit_clock_failure_is_safe_503_before_authenticator() -> None:
    authenticator = BearerSubjectAuthenticator()

    def broken_clock() -> datetime:
        raise RuntimeError("clock detail must not escape")

    response = TestClient(
        _secured_app(
            authenticator,
            clock=broken_clock,
            limit=10,
        ),
        raise_server_exceptions=False,
    ).get(
        "/protected",
        headers={"Authorization": "Bearer alice"},
    )

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "data_unavailable",
        "message": "API rate limit service is unavailable",
        "details": {},
    }
    assert "clock detail" not in response.text
    assert authenticator.calls == 0


def test_verified_principal_is_rate_limit_key_and_returns_retry_after() -> None:
    clock = MutableClock()
    authenticator = BearerSubjectAuthenticator()
    client = TestClient(_secured_app(authenticator, clock=clock))

    first_alice = client.get(
        "/protected",
        headers={"Authorization": "Bearer alice"},
    )
    first_bob = client.get(
        "/protected",
        headers={"Authorization": "Bearer bob"},
    )
    denied_alice = client.get(
        "/protected",
        headers={"Authorization": "Bearer alice"},
    )

    assert first_alice.status_code == first_bob.status_code == 200
    assert denied_alice.status_code == 429
    assert denied_alice.headers["retry-after"] == "60"
    assert denied_alice.json()["error"] == {
        "code": "rate_limited",
        "message": "API rate limit exceeded",
        "details": {},
    }

    clock.now += timedelta(seconds=60)
    assert (
        client.get(
            "/protected",
            headers={"Authorization": "Bearer alice"},
        ).status_code
        == 200
    )
    assert authenticator.calls == 3


def test_same_credential_is_pre_auth_limited_and_rotated_token_hits_principal() -> None:
    clock = MutableClock()
    authenticator = TokenMappingAuthenticator(
        {
            "alice-a": "user:alice",
            "alice-b": "user:alice",
            "bob-a": "user:bob",
        }
    )
    client = TestClient(_secured_app(authenticator, clock=clock))

    accepted = client.get(
        "/protected",
        headers={"Authorization": "Bearer alice-a"},
    )
    same_credential = client.get(
        "/protected",
        headers={"Authorization": "Bearer alice-a"},
    )
    rotated_credential = client.get(
        "/protected",
        headers={"Authorization": "Bearer alice-b"},
    )
    other_principal = client.get(
        "/protected",
        headers={"Authorization": "Bearer bob-a"},
    )

    assert accepted.status_code == other_principal.status_code == 200
    assert same_credential.status_code == rotated_credential.status_code == 429
    assert authenticator.calls == 3


def test_forwarded_identity_headers_fail_closed_and_cannot_rotate_peer() -> None:
    clock = MutableClock()
    authenticator = BearerSubjectAuthenticator()
    client = TestClient(_secured_app(authenticator, clock=clock))

    first = client.get(
        "/protected",
        headers={"X-Forwarded-For": "192.0.2.10"},
    )
    denied = client.get(
        "/protected",
        headers={
            "Forwarded": "for=198.51.100.20",
            "X-Forwarded-For": "203.0.113.30",
            "X-Real-IP": "203.0.113.31",
        },
    )

    assert first.status_code == 400
    assert first.json()["error"] == {
        "code": "invalid_input",
        "message": "Forwarded client identity headers are not accepted",
        "details": {},
    }
    assert denied.status_code == 429
    assert denied.headers["retry-after"] == "60"
    assert denied.json()["error"]["code"] == "rate_limited"
    assert authenticator.calls == 0


def test_direct_peer_edge_cap_stops_invalid_token_rotation_before_auth() -> None:
    clock = MutableClock()
    authenticator = TokenMappingAuthenticator({})
    client = TestClient(
        _secured_app(
            authenticator,
            clock=clock,
            limit=1,
            edge_limit=2,
        )
    )

    first = client.get(
        "/protected",
        headers={"Authorization": "Bearer invalid-a"},
    )
    second = client.get(
        "/protected",
        headers={"Authorization": "Bearer invalid-b"},
    )
    denied = client.get(
        "/protected",
        headers={"Authorization": "Bearer invalid-c"},
    )

    assert first.status_code == second.status_code == 401
    assert denied.status_code == 429
    assert authenticator.calls == 2


def test_denied_cors_preflight_uses_uniform_json_envelope() -> None:
    client = TestClient(
        _secured_app(
            BearerSubjectAuthenticator(),
            clock=MutableClock(),
            limit=10,
            origins=("https://console.example.com",),
        )
    )

    response = client.options(
        "/protected",
        headers={
            "Origin": "https://attacker.invalid",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"] == {
        "code": "invalid_input",
        "message": "CORS preflight request is not allowed",
        "details": {},
    }


def test_public_and_not_found_routes_cannot_bypass_global_limit() -> None:
    clock = MutableClock()
    app = _secured_app(DenyAllAuthenticator(), clock=clock)

    @app.get("/public")
    def public() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app)

    assert client.get("/public").status_code == 200
    denied = client.get("/does-not-exist")

    assert denied.status_code == 429
    assert denied.headers["retry-after"] == "60"
    assert denied.json()["error"]["code"] == "rate_limited"


def test_forged_request_state_cannot_create_a_verified_principal() -> None:
    app = FastAPI()
    install_authentication(app, DenyAllAuthenticator())

    @app.middleware("http")
    async def forge_authentication(request: object, call_next: object) -> object:
        request.state.stonks_authentication_attempted = True  # type: ignore[attr-defined]
        request.state.stonks_verified_principal = LocalPrincipal(  # type: ignore[attr-defined]
            subject="user:forged",
            roles=frozenset({Role.ADMIN}),
        )
        return await call_next(request)  # type: ignore[operator]

    @app.get("/protected")
    def protected(principal: ReadPrincipal) -> dict[str, str]:
        return {"subject": principal.subject}

    response = TestClient(app).get("/protected")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_in_memory_store_is_atomic_bounded_and_rejects_clock_regression() -> None:
    store = InMemoryRateLimitStore(max_keys=1)
    now = datetime(2026, 7, 17, tzinfo=UTC)

    first = store.consume("one", now=now, limit=1, window_seconds=60)
    denied = store.consume("one", now=now, limit=1, window_seconds=60)
    saturated = store.consume("two", now=now, limit=1, window_seconds=60)
    regressed = store.consume(
        "one",
        now=now - timedelta(microseconds=1),
        limit=1,
        window_seconds=60,
    )

    assert first.allowed is True
    assert denied.allowed is False
    assert denied.retry_after_seconds == 60
    assert saturated.allowed is False
    assert regressed.allowed is False


def test_in_memory_store_atomically_allows_only_the_configured_count() -> None:
    store = InMemoryRateLimitStore()
    now = datetime(2026, 7, 17, tzinfo=UTC)

    with ThreadPoolExecutor(max_workers=16) as executor:
        decisions = list(
            executor.map(
                lambda _: store.consume(
                    "shared",
                    now=now,
                    limit=25,
                    window_seconds=60,
                ),
                range(100),
            )
        )

    assert sum(decision.allowed for decision in decisions) == 25


def test_in_memory_store_does_not_scan_all_live_keys_per_request() -> None:
    class NoScanDict(dict[str, object]):
        def items(self):  # type: ignore[no-untyped-def]
            raise AssertionError("live key scan is forbidden")

        def __iter__(self):  # type: ignore[no-untyped-def]
            raise AssertionError("live key scan is forbidden")

    store = InMemoryRateLimitStore()
    now = datetime(2026, 7, 17, tzinfo=UTC)
    store.consume("shared", now=now, limit=1, window_seconds=60)
    store._windows = NoScanDict(store._windows)  # type: ignore[attr-defined,assignment]

    decision = store.consume("shared", now=now, limit=1, window_seconds=60)

    assert decision.allowed is False
