from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from stonks_service_auth.admission import (
    FixedWindowAdmissionStore,
    ServiceAdmissionMiddleware,
    ServiceAdmissionPolicy,
    ServiceAdmissionResponseStyle,
)

type Message = dict[str, Any]
type ASGIApp = Callable[
    [
        dict[str, Any],
        Callable[[], Awaitable[Message]],
        Callable[[Message], Awaitable[None]],
    ],
    Awaitable[None],
]


def _scope(
    *,
    client: str = "127.0.0.1",
    authorization: bytes | None = b"Bearer token-a",
    extra_headers: tuple[tuple[bytes, bytes], ...] = (),
) -> dict[str, Any]:
    headers = list(extra_headers)
    if authorization is not None:
        headers.append((b"authorization", authorization))
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/work",
        "raw_path": b"/v1/work",
        "query_string": b"",
        "headers": headers,
        "client": (client, 50000),
        "server": ("127.0.0.1", 7200),
    }


async def _request(
    app: ASGIApp,
    scope: dict[str, Any],
) -> list[Message]:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


def _headers(messages: list[Message]) -> dict[bytes, bytes]:
    return dict(messages[0]["headers"])


def _body(messages: list[Message]) -> dict[str, Any]:
    return json.loads(messages[1]["body"])


@pytest.mark.asyncio
async def test_direct_peer_edge_stops_credential_rotation_before_downstream() -> None:
    calls = 0

    async def downstream(
        _scope: dict[str, Any],
        _receive: Callable[[], Awaitable[Message]],
        send: Callable[[Message], Awaitable[None]],
    ) -> None:
        nonlocal calls
        calls += 1
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = ServiceAdmissionMiddleware(
        downstream,
        policy=ServiceAdmissionPolicy(
            direct_peer_requests=2,
            credential_requests=100,
            window_seconds=60,
            max_keys=32,
        ),
        clock=lambda: 1.0,
    )

    first = await _request(app, _scope(authorization=b"Bearer token-a"))
    second = await _request(app, _scope(authorization=b"Bearer token-b"))
    denied = await _request(app, _scope(authorization=b"Bearer token-c"))

    assert first[0]["status"] == second[0]["status"] == 204
    assert denied[0]["status"] == 429
    assert _body(denied)["error"]["code"] == "rate_limited"
    assert _headers(denied)[b"retry-after"] == b"59"
    assert calls == 2


@pytest.mark.asyncio
async def test_credential_fingerprint_stops_peer_rotation_without_storing_secret() -> (
    None
):
    calls = 0

    async def downstream(
        _scope: dict[str, Any],
        _receive: Callable[[], Awaitable[Message]],
        send: Callable[[Message], Awaitable[None]],
    ) -> None:
        nonlocal calls
        calls += 1
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    store = FixedWindowAdmissionStore(max_keys=32)
    app = ServiceAdmissionMiddleware(
        downstream,
        policy=ServiceAdmissionPolicy(
            direct_peer_requests=100,
            credential_requests=1,
            window_seconds=60,
            max_keys=32,
        ),
        clock=lambda: 1.0,
        store=store,
    )

    accepted = await _request(app, _scope(client="127.0.0.1"))
    denied = await _request(app, _scope(client="127.0.0.2"))

    assert accepted[0]["status"] == 204
    assert denied[0]["status"] == 429
    assert calls == 1
    assert all("token-a" not in key for key in store.active_keys)


@pytest.mark.parametrize(
    "header",
    [
        b"forwarded",
        b"x-forwarded-for",
        b"x-forwarded-host",
        b"x-forwarded-proto",
        b"x-forwarded-port",
        b"x-real-ip",
    ],
)
@pytest.mark.asyncio
async def test_forwarded_identity_headers_fail_closed_and_consume_edge_budget(
    header: bytes,
) -> None:
    calls = 0

    async def downstream(
        _scope: dict[str, Any],
        _receive: Callable[[], Awaitable[Message]],
        _send: Callable[[Message], Awaitable[None]],
    ) -> None:
        nonlocal calls
        calls += 1

    app = ServiceAdmissionMiddleware(
        downstream,
        policy=ServiceAdmissionPolicy(
            direct_peer_requests=1,
            credential_requests=100,
            window_seconds=60,
            max_keys=16,
        ),
        clock=lambda: 1.0,
    )
    scope = _scope(extra_headers=((header, b"attacker"),))

    rejected = await _request(app, scope)
    rate_limited = await _request(app, scope)

    assert rejected[0]["status"] == 400
    assert _body(rejected)["error"]["code"] == "invalid_request"
    assert rate_limited[0]["status"] == 429
    assert calls == 0


def test_fixed_window_store_is_bounded_and_reuses_expired_capacity() -> None:
    store = FixedWindowAdmissionStore(max_keys=1)

    accepted = store.consume("key-a", now=1.0, limit=10, window_seconds=10)
    denied = store.consume("key-b", now=1.0, limit=10, window_seconds=10)
    after_expiry = store.consume("key-b", now=10.0, limit=10, window_seconds=10)

    assert accepted.allowed is True
    assert denied.allowed is False
    assert denied.retry_after_seconds == 9
    assert after_expiry.allowed is True
    assert store.active_keys == ("key-b",)


@pytest.mark.asyncio
async def test_fixed_window_resets_and_openbb_rejection_stays_safe() -> None:
    now = [1.0]

    async def downstream(
        _scope: dict[str, Any],
        _receive: Callable[[], Awaitable[Message]],
        send: Callable[[Message], Awaitable[None]],
    ) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = ServiceAdmissionMiddleware(
        downstream,
        policy=ServiceAdmissionPolicy(
            direct_peer_requests=1,
            credential_requests=1,
            window_seconds=60,
            max_keys=8,
        ),
        clock=lambda: now[0],
        response_style=ServiceAdmissionResponseStyle.OPENBB,
        extra_response_headers=((b"link", b"</source>; rel=source"),),
    )

    accepted = await _request(app, _scope())
    denied = await _request(app, _scope())
    now[0] = 60.0
    reset = await _request(app, _scope())

    assert accepted[0]["status"] == reset[0]["status"] == 204
    assert denied[0]["status"] == 429
    assert _body(denied) == {"detail": "Service admission rate limit exceeded"}
    assert _headers(denied)[b"cache-control"] == b"no-store"
    assert _headers(denied)[b"x-content-type-options"] == b"nosniff"
    assert _headers(denied)[b"link"] == b"</source>; rel=source"


@pytest.mark.parametrize(
    "overrides",
    [
        {"direct_peer_requests": 0},
        {"credential_requests": 0},
        {"window_seconds": 0},
        {"max_keys": 0},
    ],
)
def test_service_admission_policy_rejects_invalid_limits(
    overrides: dict[str, int],
) -> None:
    values = {
        "direct_peer_requests": 100,
        "credential_requests": 50,
        "window_seconds": 60,
        "max_keys": 128,
        **overrides,
    }

    with pytest.raises(ValueError):
        ServiceAdmissionPolicy(**values)
