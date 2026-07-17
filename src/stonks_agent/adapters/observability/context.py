"""Request-safe trace context binding and secure identifier creation."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Protocol

from stonks_agent.domain.telemetry import (
    CorrelationContext,
    TraceCarrier,
    TraceContext,
)

_CURRENT_TRACE: ContextVar[TraceContext | None] = ContextVar(
    "stonks_trace_context",
    default=None,
)


class TraceIdGenerator(Protocol):
    def new_trace_id(self) -> str: ...

    def new_span_id(self) -> str: ...


class SecureTraceIdGenerator:
    __slots__ = ()

    def new_trace_id(self) -> str:
        return secrets.token_hex(16)

    def new_span_id(self) -> str:
        return secrets.token_hex(8)


def current_trace_context() -> TraceContext | None:
    return _CURRENT_TRACE.get()


def current_trace_carrier() -> TraceCarrier | None:
    context = current_trace_context()
    return None if context is None else context.to_carrier()


def bind_trace_context(
    context: TraceContext,
) -> Token[TraceContext | None]:
    return _CURRENT_TRACE.set(context)


def reset_trace_context(token: Token[TraceContext | None]) -> None:
    _CURRENT_TRACE.reset(token)


@contextmanager
def trace_scope(context: TraceContext) -> Iterator[TraceContext]:
    token = bind_trace_context(context)
    try:
        yield context
    finally:
        reset_trace_context(token)


def create_trace_context(
    *,
    parent: TraceCarrier | None = None,
    correlation: CorrelationContext | None = None,
    generator: TraceIdGenerator | None = None,
) -> TraceContext:
    selected_generator = generator or SecureTraceIdGenerator()
    selected_correlation = correlation or CorrelationContext()
    try:
        return TraceContext(
            trace_id=(
                parent.trace_id
                if parent is not None
                else selected_generator.new_trace_id()
            ),
            span_id=selected_generator.new_span_id(),
            trace_flags=parent.trace_flags if parent is not None else "01",
            tracestate=parent.tracestate if parent is not None else None,
            **selected_correlation.model_dump(),
        )
    except Exception:
        raise ValueError("trace identifier generator returned invalid output") from None
