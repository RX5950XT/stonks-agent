from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

import pytest

from stonks_agent.adapters.market_data.openbb_rest import (
    _dispatch_payload,
    _OpenBBQuery,
)
from stonks_service_auth import (
    ServiceAccessTarget,
    ServiceIdentity,
    ServicePermission,
    ServicePrincipal,
    ServiceReceiver,
    ServiceResourceKind,
    canonical_request_hash,
)

ROOT = Path(__file__).resolve().parents[2]
SURFACE = ROOT / "sidecars" / "openbb" / "surface.py"
HISTORICAL = "/api/v1/equity/price/historical"
AUTHORIZATION = "Bearer test-core-runner-credential-32-bytes"
DEFAULT_QUERY = {"provider": "yfinance", "symbol": "AAPL"}


def market_target(identifier: object) -> ServiceAccessTarget:
    return ServiceAccessTarget(
        kind=ServiceResourceKind.MARKET,
        identifier=str(identifier),
    )


class ExactServiceAuthenticator:
    def __init__(
        self,
        targets: tuple[ServiceAccessTarget, ...],
        *,
        receiver: ServiceReceiver = ServiceReceiver.OPENBB,
        generation: int = 0,
        query: dict[str, str] | None = None,
        nonce_hash: str | None = None,
        expires_at: int = 1_900_000_000,
    ) -> None:
        request_payload: dict[str, object] = {
            "method": "GET",
            "path": HISTORICAL,
            "query": query or DEFAULT_QUERY,
        }
        request_hash = canonical_request_hash(request_payload)
        self._principal = ServicePrincipal(
            subject="service:core-runner",
            identity=ServiceIdentity.CORE_RUNNER,
            receiver=receiver,
            permission=ServicePermission.DISPATCH_ASSIGNED_MARKET_DATA,
            targets=frozenset(targets),
            attempt_generation=generation,
            attempt_nonce_hash=nonce_hash or request_hash,
            request_hash=request_hash,
            token_id="openbb-test-token",
            issued_at=1_800_000_000,
            expires_at=expires_at,
        )

    def authenticate(self, authorization: str | None) -> ServicePrincipal | None:
        return self._principal if authorization == AUTHORIZATION else None


def _load_surface() -> Any:
    spec = spec_from_file_location("openbb_surface_under_test", SURFACE)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _exercise(
    middleware: Any,
    *,
    scope_type: str = "http",
    method: str = "GET",
    path: str = HISTORICAL,
    query_string: bytes = b"symbol=AAPL&provider=yfinance",
    headers: tuple[tuple[bytes, bytes], ...] = (),
) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": f"{scope_type}.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": scope_type,
        "method": method,
        "path": path,
        "query_string": query_string,
        "headers": list(headers),
    }
    asyncio.run(middleware(scope, receive, send))
    return sent


def authorization_headers(
    authorization: str = AUTHORIZATION,
) -> tuple[tuple[bytes, bytes], ...]:
    return ((b"authorization", authorization.encode("ascii")),)


@pytest.mark.parametrize("path", ["/healthz", "/source"])
def test_anonymous_legal_and_health_surface_reaches_downstream(path: str) -> None:
    surface = _load_surface()
    calls: list[dict[str, Any]] = []

    async def downstream(
        scope: dict[str, Any],
        _receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        calls.append(scope)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent = _exercise(
        surface.SurfaceAllowlist(
            downstream,
            authenticator=ExactServiceAuthenticator((market_target("US/AAPL"),)),
        ),
        path=path,
    )

    assert len(calls) == 1
    assert sent[0]["status"] == 204


def test_historical_surface_requires_exact_market_target() -> None:
    surface = _load_surface()
    calls: list[dict[str, Any]] = []

    async def downstream(
        scope: dict[str, Any],
        _receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        calls.append(scope)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent = _exercise(
        surface.SurfaceAllowlist(
            downstream,
            authenticator=ExactServiceAuthenticator((market_target("US/AAPL"),)),
        ),
        headers=authorization_headers(),
    )

    assert len(calls) == 1
    assert sent[0]["status"] == 204


def test_core_and_sidecar_use_the_same_canonical_request_payload() -> None:
    surface = _load_surface()
    parsed = surface._target_from_scope(
        {
            "query_string": (
                b"end_date=2026-01-02&provider=yfinance&symbol=AAPL"
                b"&start_date=2026-01-01"
            )
        }
    )

    assert parsed is not None
    _target, payload = parsed
    assert payload == _dispatch_payload(
        _OpenBBQuery(
            symbol="AAPL",
            start_date="2026-01-01",
            end_date="2026-01-02",
        )
    )


@pytest.mark.parametrize(
    ("authorization", "target", "expected_status"),
    [
        (None, "US/AAPL", 401),
        ("Bearer invalid-service-credential", "US/AAPL", 401),
        (AUTHORIZATION, "US/MSFT", 403),
    ],
)
def test_historical_surface_fails_closed_before_openbb(
    authorization: str | None,
    target: str,
    expected_status: int,
) -> None:
    surface = _load_surface()
    calls = 0

    async def downstream(*_args: object) -> None:
        nonlocal calls
        calls += 1

    headers = authorization_headers(authorization) if authorization is not None else ()
    sent = _exercise(
        surface.SurfaceAllowlist(
            downstream,
            authenticator=ExactServiceAuthenticator((market_target(target),)),
        ),
        headers=headers,
    )

    assert calls == 0
    assert sent[0]["status"] == expected_status
    assert (b"link", surface.SOURCE_LINK.encode("ascii")) in sent[0]["headers"]
    if expected_status == 401:
        assert (b"www-authenticate", b"Bearer") in sent[0]["headers"]


def test_authentication_happens_before_query_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    surface = _load_surface()
    target_calls = 0

    def forbidden_query_parse(_scope: object) -> object:
        nonlocal target_calls
        target_calls += 1
        raise AssertionError("query must not be processed before authentication")

    monkeypatch.setattr(surface, "_target_from_scope", forbidden_query_parse)

    sent = _exercise(
        surface.SurfaceAllowlist(
            lambda *_args: None,
            authenticator=ExactServiceAuthenticator((market_target("US/AAPL"),)),
        ),
        query_string=b"%ZZ=" + b"x" * 10_000,
    )

    assert target_calls == 0
    assert sent[0]["status"] == 401


@pytest.mark.parametrize(
    "headers",
    [
        authorization_headers() + authorization_headers(),
        ((b"authorization", b" Bearer leading-space"),),
        ((b"authorization", b"Bearer non-ascii-\xff"),),
        ((b"authorization", b"Bearer " + b"x" * 4097),),
    ],
)
def test_ambiguous_or_malformed_authorization_fails_before_query_processing(
    headers: tuple[tuple[bytes, bytes], ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    surface = _load_surface()
    target_calls = 0

    def forbidden_query_parse(_scope: object) -> object:
        nonlocal target_calls
        target_calls += 1
        raise AssertionError("query must not be processed before authentication")

    monkeypatch.setattr(surface, "_target_from_scope", forbidden_query_parse)

    sent = _exercise(
        surface.SurfaceAllowlist(
            lambda *_args: None,
            authenticator=ExactServiceAuthenticator((market_target("US/AAPL"),)),
        ),
        query_string=b"%ZZ=" + b"x" * 10_000,
        headers=headers,
    )

    assert target_calls == 0
    assert sent[0]["status"] == 401


@pytest.mark.parametrize(
    "query_string",
    [
        b"symbol=AAPL",
        b"symbol=AAPL&provider=fmp",
        b"symbol=AAPL&symbol=MSFT&provider=yfinance",
        b"symbol=AAPL%2FBAD&provider=yfinance",
        b"symbol=AAPL&provider=yfinance&unexpected=value",
        b"x=" + b"a" * 4096,
    ],
)
def test_authenticated_invalid_or_ambiguous_query_fails_closed(
    query_string: bytes,
) -> None:
    surface = _load_surface()
    calls = 0

    async def downstream(*_args: object) -> None:
        nonlocal calls
        calls += 1

    sent = _exercise(
        surface.SurfaceAllowlist(
            downstream,
            authenticator=ExactServiceAuthenticator((market_target("US/AAPL"),)),
        ),
        query_string=query_string,
        headers=authorization_headers(),
    )

    assert calls == 0
    assert sent[0]["status"] == 400
    assert (b"link", surface.SOURCE_LINK.encode("ascii")) in sent[0]["headers"]


@pytest.mark.parametrize(
    "authenticator",
    [
        ExactServiceAuthenticator(
            (market_target("US/AAPL"),),
            receiver=ServiceReceiver.KRONOS,
        ),
        ExactServiceAuthenticator(
            (market_target("US/AAPL"),),
            generation=1,
        ),
        ExactServiceAuthenticator(
            (market_target("US/AAPL"),),
            nonce_hash="a" * 64,
        ),
        ExactServiceAuthenticator(
            (market_target("US/AAPL"),),
            query={"provider": "yfinance", "symbol": "MSFT"},
        ),
    ],
)
def test_receiver_attempt_nonce_and_request_are_token_bound(
    authenticator: ExactServiceAuthenticator,
) -> None:
    surface = _load_surface()
    calls = 0

    async def downstream(*_args: object) -> None:
        nonlocal calls
        calls += 1

    sent = _exercise(
        surface.SurfaceAllowlist(downstream, authenticator=authenticator),
        headers=authorization_headers(),
    )

    assert calls == 0
    assert sent[0]["status"] == 403


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", HISTORICAL),
        ("OPTIONS", HISTORICAL),
        ("HEAD", "/healthz"),
        ("GET", f"{HISTORICAL}/"),
        ("GET", "/api/v1/equity/price/quote"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
        ("GET", "/"),
    ],
)
def test_every_other_http_request_is_hidden_before_downstream(
    method: str,
    path: str,
) -> None:
    surface = _load_surface()
    calls = 0

    async def downstream(*_args: object) -> None:
        nonlocal calls
        calls += 1

    sent = _exercise(
        surface.SurfaceAllowlist(
            downstream,
            authenticator=ExactServiceAuthenticator((market_target("US/AAPL"),)),
        ),
        method=method,
        path=path,
    )

    assert calls == 0
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 404
    assert (b"link", surface.SOURCE_LINK.encode("ascii")) in sent[0]["headers"]
    assert sent[1] == {
        "type": "http.response.body",
        "body": b'{"detail":"Not Found"}',
    }


def test_websocket_is_closed_without_reaching_downstream() -> None:
    surface = _load_surface()
    calls = 0

    async def downstream(*_args: object) -> None:
        nonlocal calls
        calls += 1

    sent = _exercise(
        surface.SurfaceAllowlist(
            downstream,
            authenticator=ExactServiceAuthenticator((market_target("US/AAPL"),)),
        ),
        scope_type="websocket",
        path=HISTORICAL,
    )

    assert calls == 0
    assert sent == [{"type": "websocket.close", "code": 1008}]


def test_lifespan_only_is_forwarded_to_downstream() -> None:
    surface = _load_surface()
    calls = 0

    async def downstream(*_args: object) -> None:
        nonlocal calls
        calls += 1

    sent = _exercise(
        surface.SurfaceAllowlist(
            downstream,
            authenticator=ExactServiceAuthenticator((market_target("US/AAPL"),)),
        ),
        scope_type="lifespan",
        path="",
    )

    assert calls == 1
    assert sent == []
