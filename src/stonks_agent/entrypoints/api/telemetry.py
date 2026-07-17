"""Outer API trace/correlation boundary with best-effort instrumentation."""

from __future__ import annotations

import secrets
from asyncio import Future, ensure_future
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from stonks_agent.adapters.observability.context import (
    SecureTraceIdGenerator,
    TraceIdGenerator,
    create_trace_context,
    trace_scope,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.telemetry import (
    ComponentName,
    CorrelationContext,
    OperationName,
    TraceCarrier,
    TraceContext,
)
from stonks_agent.entrypoints.api.envelope import error_envelope
from stonks_agent.entrypoints.api.web_protection import SECURITY_RESPONSE_HEADERS
from stonks_agent.ports.telemetry import OperationRecorderPort

type RequestIdFactory = Callable[[], str]

_INSTALLED_STATE_KEY = "_stonks_api_telemetry_options"
_TRACE_HEADERS = frozenset({b"traceparent", b"tracestate", b"x-request-id"})
_DEFAULT_GENERATOR = SecureTraceIdGenerator()


def _secure_request_id() -> str:
    return secrets.token_hex(16)


@dataclass(frozen=True, slots=True)
class ApiTelemetryOptions:
    generator: TraceIdGenerator = field(default_factory=lambda: _DEFAULT_GENERATOR)
    request_id_factory: RequestIdFactory = _secure_request_id
    recorder: OperationRecorderPort | None = None


def install_api_telemetry(
    app: FastAPI,
    *,
    options: ApiTelemetryOptions | None = None,
) -> None:
    runtime = options or ApiTelemetryOptions()
    installed = getattr(app.state, _INSTALLED_STATE_KEY, None)
    if installed is not None:
        if not _same_options(installed, runtime):
            raise ValueError("API telemetry is already configured differently")
        return
    setattr(app.state, _INSTALLED_STATE_KEY, runtime)
    app.add_middleware(_ApiTelemetryMiddleware, options=runtime)


class _ApiTelemetryMiddleware:
    def __init__(self, app: ASGIApp, *, options: ApiTelemetryOptions) -> None:
        self._app = app
        self._options = options

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        parent, request_id, invalid = _request_context(scope, self._options)
        context = _create_context(
            parent=None if invalid else parent,
            correlation=CorrelationContext(request_id=request_id),
            generator=self._options.generator,
        )
        with trace_scope(context):
            if invalid:

                async def reject() -> Result[None]:
                    await _send_invalid_context(scope, receive, send, context)
                    return _http_result(400)

                await _record_async(self._options.recorder, reject, context)
                return
            status: int | None = None

            async def send_with_context(message: Message) -> None:
                nonlocal status
                if message["type"] != "http.response.start":
                    await send(message)
                    return
                status = int(message["status"])
                updated = dict(message)
                updated["headers"] = _response_headers(message, context)
                await send(updated)

            async def dispatch() -> Result[None]:
                await self._app(scope, receive, send_with_context)
                return _http_result(status or 500)

            await _record_async(self._options.recorder, dispatch, context)


def _request_context(
    scope: Scope,
    options: ApiTelemetryOptions,
) -> tuple[TraceCarrier | None, str, bool]:
    parent, trace_invalid = _parent_carrier(scope)
    request_id, request_invalid = _request_id(scope, options.request_id_factory)
    return parent, request_id, trace_invalid or request_invalid


def _create_context(
    *,
    parent: TraceCarrier | None,
    correlation: CorrelationContext,
    generator: TraceIdGenerator,
) -> TraceContext:
    try:
        return create_trace_context(
            parent=parent,
            correlation=correlation,
            generator=generator,
        )
    except Exception:
        return create_trace_context(
            parent=parent,
            correlation=correlation,
        )


def _parent_carrier(scope: Scope) -> tuple[TraceCarrier | None, bool]:
    traceparents = _header_values(scope, b"traceparent")
    tracestates = _header_values(scope, b"tracestate")
    if len(traceparents) > 1 or len(tracestates) > 1:
        return None, True
    if not traceparents:
        return None, bool(tracestates)
    traceparent = _decode_ascii(traceparents[0])
    tracestate = _decode_ascii(tracestates[0]) if tracestates else None
    if traceparent is None or (tracestates and tracestate is None):
        return None, True
    try:
        return TraceCarrier(
            traceparent=traceparent,
            tracestate=tracestate,
        ), False
    except ValidationError:
        return None, True


def _request_id(
    scope: Scope,
    factory: RequestIdFactory,
) -> tuple[str, bool]:
    values = _header_values(scope, b"x-request-id")
    if len(values) > 1:
        return _new_request_id(factory), True
    if not values:
        return _new_request_id(factory), False
    selected = _decode_ascii(values[0])
    if selected is None:
        return _new_request_id(factory), True
    try:
        CorrelationContext(request_id=selected)
    except ValidationError:
        return _new_request_id(factory), True
    return selected, False


def _new_request_id(factory: RequestIdFactory) -> str:
    try:
        selected = factory()
        CorrelationContext(request_id=selected)
        return selected
    except Exception:
        return _secure_request_id()


async def _send_invalid_context(
    scope: Scope,
    receive: Receive,
    send: Send,
    context: TraceContext,
) -> None:
    envelope = error_envelope(
        StructuredError(
            code=ErrorCode.INVALID_INPUT,
            message="Trace context is invalid",
        )
    )
    response = JSONResponse(
        status_code=envelope.status,
        content=envelope.model_dump(mode="json"),
        headers={
            **SECURITY_RESPONSE_HEADERS,
            **context.to_carrier().to_headers(),
            "x-request-id": context.request_id or _secure_request_id(),
        },
    )
    await response(scope, receive, send)


def _response_headers(
    message: Message, context: TraceContext
) -> list[tuple[bytes, bytes]]:
    preserved = [
        (name, value)
        for name, value in message.get("headers", [])
        if name.lower() not in _TRACE_HEADERS
    ]
    carrier = context.to_carrier()
    selected = [
        (b"traceparent", carrier.traceparent.encode("ascii")),
        (b"x-request-id", (context.request_id or _secure_request_id()).encode("ascii")),
    ]
    if carrier.tracestate is not None:
        selected.append((b"tracestate", carrier.tracestate.encode("ascii")))
    return [*preserved, *selected]


def _header_values(scope: Scope, name: bytes) -> list[bytes]:
    return [value for key, value in scope["headers"] if key.lower() == name]


def _decode_ascii(value: bytes) -> str | None:
    try:
        return value.decode("ascii")
    except UnicodeDecodeError:
        return None


async def _record_async(
    recorder: OperationRecorderPort | None,
    call: Callable[[], Awaitable[Result[None]]],
    context: TraceContext,
) -> Result[None]:
    if recorder is None:
        return await call()
    task: Future[Result[None]] | None = None

    async def invoke() -> Result[None]:
        nonlocal task
        if task is None:
            task = ensure_future(call())
        return await task

    with suppress(Exception):
        await recorder.record_async_result(
            component=ComponentName.API,
            operation=OperationName.HTTP_REQUEST,
            call=invoke,
            parent=context,
        )
    return await invoke()


def _http_result(status: int) -> Result[None]:
    if status < 400:
        return Success(None)
    return Failure(
        StructuredError(
            code=_status_code(status),
            message="HTTP request failed",
        )
    )


def _status_code(status: int) -> ErrorCode:
    return {
        400: ErrorCode.INVALID_INPUT,
        401: ErrorCode.UNAUTHORIZED,
        403: ErrorCode.FORBIDDEN,
        404: ErrorCode.NOT_FOUND,
        409: ErrorCode.CONFLICT,
        413: ErrorCode.PAYLOAD_TOO_LARGE,
        429: ErrorCode.RATE_LIMITED,
        503: ErrorCode.DATA_UNAVAILABLE,
    }.get(status, ErrorCode.INTERNAL_ERROR)


def _same_options(left: object, right: ApiTelemetryOptions) -> bool:
    return (
        isinstance(left, ApiTelemetryOptions)
        and left.generator is right.generator
        and left.request_id_factory is right.request_id_factory
        and left.recorder is right.recorder
    )
