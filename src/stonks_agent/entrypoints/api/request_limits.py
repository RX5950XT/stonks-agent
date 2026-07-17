"""Bounded ASGI request bodies with uniform structured errors."""

from __future__ import annotations

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from stonks_agent.domain.errors import ErrorCode, StructuredError
from stonks_agent.entrypoints.api.envelope import error_envelope

_FORWARDED_IDENTITY_HEADERS = frozenset(
    {
        b"forwarded",
        b"x-forwarded-for",
        b"x-real-ip",
    }
)


class ForwardedHeaderRejectMiddleware:
    """Reject proxy-derived client identity until an explicit trust policy exists."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        if any(
            name.lower() in _FORWARDED_IDENTITY_HEADERS for name, _ in scope["headers"]
        ):
            await _send_forwarded_rejection(scope, receive, send)
            return
        await self._app(scope, receive, send)


class RequestBodyLimitMiddleware:
    """Reject declared or streamed bodies before unbounded buffering."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int,
        max_frames: int = 256,
    ) -> None:
        if isinstance(max_bytes, bool) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        if isinstance(max_frames, bool) or not 1 <= max_frames <= 4096:
            raise ValueError("max_frames must be between 1 and 4096")
        self._app = app
        self._max_bytes = max_bytes
        self._max_frames = max_frames

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        if _declared_size_exceeds(scope, self._max_bytes):
            await _send_rejection(scope, receive, send)
            return
        buffered = await _buffer_request(
            receive,
            self._max_bytes,
            self._max_frames,
        )
        if buffered is None:
            await _send_rejection(scope, receive, send)
            return
        if not buffered:
            return
        index = 0

        async def replay_receive() -> Message:
            nonlocal index
            if index < len(buffered):
                message = buffered[index]
                index += 1
                return message
            return await receive()

        await self._app(scope, replay_receive, send)


async def _buffer_request(
    receive: Receive,
    maximum: int,
    maximum_frames: int,
) -> tuple[Message, ...] | None:
    messages: list[Message] = []
    consumed = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return ()
        if len(messages) >= maximum_frames:
            return None
        messages.append(message)
        if message["type"] != "http.request":
            return tuple(messages)
        consumed += len(message.get("body", b""))
        if consumed > maximum:
            return None
        if not message.get("more_body", False):
            return tuple(messages)


def _declared_size_exceeds(scope: Scope, maximum: int) -> bool:
    lengths = [value for name, value in scope["headers"] if name == b"content-length"]
    if not lengths:
        return False
    if len(lengths) != 1:
        return True
    value = lengths[0]
    if not value.isascii() or not value.isdigit():
        return True
    limit = str(maximum).encode("ascii")
    return len(value) > len(limit) or (len(value) == len(limit) and value > limit)


async def _send_rejection(scope: Scope, receive: Receive, send: Send) -> None:
    envelope = error_envelope(
        StructuredError(
            code=ErrorCode.PAYLOAD_TOO_LARGE,
            message="Request body exceeds the allowed size",
        )
    )
    response = JSONResponse(
        status_code=envelope.status,
        content=envelope.model_dump(mode="json"),
    )
    await response(scope, receive, send)


async def _send_forwarded_rejection(
    scope: Scope,
    receive: Receive,
    send: Send,
) -> None:
    envelope = error_envelope(
        StructuredError(
            code=ErrorCode.INVALID_INPUT,
            message="Forwarded client identity headers are not accepted",
        )
    )
    response = JSONResponse(
        status_code=envelope.status,
        content=envelope.model_dump(mode="json"),
    )
    await response(scope, receive, send)
