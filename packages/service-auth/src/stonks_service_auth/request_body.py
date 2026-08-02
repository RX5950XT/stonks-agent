"""Bounded ASGI request-body reader for isolated service ingress."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

DEFAULT_MAX_REQUEST_FRAMES = 256
DEFAULT_REQUEST_BODY_TIMEOUT_SECONDS = 5.0

type ASGIReceive = Callable[[], Awaitable[MutableMapping[str, Any]]]
type MonotonicClock = Callable[[], float]


class RequestBodyReadError(Exception):
    """Base class for safe, typed request-body failures."""

    status_code: int
    code: str
    safe_message: str


class RequestBodyTooLargeError(RequestBodyReadError):
    """The byte or frame budget was exhausted."""

    status_code = 413
    code = "request_too_large"
    safe_message = "Request body exceeds the configured limit"


class RequestBodyTimeoutError(RequestBodyReadError):
    """The total monotonic body deadline expired."""

    status_code = 408
    code = "request_timeout"
    safe_message = "Request body deadline exceeded"


class RequestBodyProtocolError(RequestBodyReadError):
    """The ASGI body stream disconnected or was malformed."""

    status_code = 400
    code = "invalid_request"
    safe_message = "Request body is invalid"


async def read_bounded_request_body(
    receive: ASGIReceive,
    *,
    max_bytes: int,
    max_frames: int = DEFAULT_MAX_REQUEST_FRAMES,
    timeout_seconds: float = DEFAULT_REQUEST_BODY_TIMEOUT_SECONDS,
    monotonic: MonotonicClock = time.monotonic,
) -> bytes:
    """Read one ASGI HTTP body within byte, frame, and total-time budgets."""

    _validate_limits(
        max_bytes=max_bytes,
        max_frames=max_frames,
        timeout_seconds=timeout_seconds,
    )
    deadline = monotonic() + timeout_seconds
    body = bytearray()

    for _frame_number in range(max_frames):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise RequestBodyTimeoutError
        try:
            message = await asyncio.wait_for(receive(), timeout=remaining)
        except TimeoutError:
            raise RequestBodyTimeoutError from None
        if message.get("type") != "http.request":
            raise RequestBodyProtocolError
        chunk = message.get("body", b"")
        more_body = message.get("more_body", False)
        if not isinstance(chunk, bytes) or not isinstance(more_body, bool):
            raise RequestBodyProtocolError
        if len(chunk) > max_bytes - len(body):
            raise RequestBodyTooLargeError
        body.extend(chunk)
        if not more_body:
            return bytes(body)

    raise RequestBodyTooLargeError


def _validate_limits(
    *,
    max_bytes: int,
    max_frames: int,
    timeout_seconds: float,
) -> None:
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    if not 1 <= max_frames <= 4_096:
        raise ValueError("max_frames is outside the supported range")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
