from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest

from stonks_service_auth.request_body import (
    RequestBodyProtocolError,
    RequestBodyTimeoutError,
    RequestBodyTooLargeError,
    read_bounded_request_body,
)

type Receive = Callable[[], Awaitable[dict[str, Any]]]


def _receive(
    messages: list[dict[str, Any]],
) -> tuple[Receive, list[dict[str, Any]]]:
    remaining = list(messages)

    async def receive() -> dict[str, Any]:
        return remaining.pop(0)

    return receive, remaining


@pytest.mark.asyncio
async def test_bounded_body_accepts_legitimate_chunked_json_at_exact_limit() -> None:
    receive, remaining = _receive(
        [
            {"type": "http.request", "body": b'{"ok"', "more_body": True},
            {"type": "http.request", "body": b":true}", "more_body": False},
        ]
    )

    body = await read_bounded_request_body(
        receive,
        max_bytes=11,
        max_frames=2,
        timeout_seconds=1.0,
    )

    assert body == b'{"ok":true}'
    assert remaining == []


@pytest.mark.asyncio
async def test_bounded_body_rejects_bytes_beyond_limit_without_draining() -> None:
    receive, remaining = _receive(
        [
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"5", "more_body": True},
            {"type": "http.request", "body": b"ignored", "more_body": False},
        ]
    )

    with pytest.raises(RequestBodyTooLargeError):
        await read_bounded_request_body(
            receive,
            max_bytes=4,
            max_frames=8,
            timeout_seconds=1.0,
        )

    assert len(remaining) == 1


@pytest.mark.asyncio
async def test_bounded_body_counts_zero_length_asgi_frames() -> None:
    receive, remaining = _receive(
        [
            {"type": "http.request", "body": b"", "more_body": True},
            {"type": "http.request", "body": b"", "more_body": True},
            {"type": "http.request", "body": b"", "more_body": True},
            {"type": "http.request", "body": b"{}", "more_body": False},
        ]
    )

    with pytest.raises(RequestBodyTooLargeError):
        await read_bounded_request_body(
            receive,
            max_bytes=16,
            max_frames=2,
            timeout_seconds=1.0,
        )

    assert len(remaining) == 2


@pytest.mark.asyncio
async def test_bounded_body_enforces_one_monotonic_total_deadline() -> None:
    receive, remaining = _receive(
        [
            {"type": "http.request", "body": b"{", "more_body": True},
            {"type": "http.request", "body": b"}", "more_body": False},
        ]
    )
    ticks: Iterator[float] = iter((10.0, 10.0, 11.1))

    with pytest.raises(RequestBodyTimeoutError):
        await read_bounded_request_body(
            receive,
            max_bytes=16,
            max_frames=4,
            timeout_seconds=1.0,
            monotonic=lambda: next(ticks),
        )

    assert len(remaining) == 1


@pytest.mark.asyncio
async def test_bounded_body_rejects_disconnect_and_invalid_message() -> None:
    disconnected, _ = _receive([{"type": "http.disconnect"}])
    invalid, _ = _receive(
        [{"type": "websocket.receive", "bytes": b"{}", "more_body": False}]
    )

    with pytest.raises(RequestBodyProtocolError):
        await read_bounded_request_body(
            disconnected,
            max_bytes=16,
            max_frames=4,
            timeout_seconds=1.0,
        )
    with pytest.raises(RequestBodyProtocolError):
        await read_bounded_request_body(
            invalid,
            max_bytes=16,
            max_frames=4,
            timeout_seconds=1.0,
        )


@pytest.mark.parametrize(
    ("max_bytes", "max_frames", "timeout_seconds"),
    [(0, 1, 1.0), (1, 0, 1.0), (1, 1, 0.0)],
)
@pytest.mark.asyncio
async def test_bounded_body_rejects_invalid_security_limits(
    max_bytes: int,
    max_frames: int,
    timeout_seconds: float,
) -> None:
    receive, _ = _receive([{"type": "http.request", "body": b"", "more_body": False}])

    with pytest.raises(ValueError):
        await read_bounded_request_body(
            receive,
            max_bytes=max_bytes,
            max_frames=max_frames,
            timeout_seconds=timeout_seconds,
        )
