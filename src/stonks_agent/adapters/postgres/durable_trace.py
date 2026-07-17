"""Typed serialization helpers for non-canonical durable trace columns."""

from __future__ import annotations

from pydantic import ValidationError

from stonks_agent.adapters.observability.context import current_trace_context
from stonks_agent.domain.telemetry import TraceCarrier


def trace_carrier_from_columns(
    traceparent: str | None,
    tracestate: str | None,
) -> TraceCarrier | None:
    if traceparent is None:
        if tracestate is not None:
            raise ValueError("durable tracestate is missing traceparent")
        return None
    try:
        return TraceCarrier(traceparent=traceparent, tracestate=tracestate)
    except ValidationError as error:
        raise ValueError("durable trace context is invalid") from error


def current_durable_trace() -> tuple[TraceCarrier | None, str | None]:
    context = current_trace_context()
    if context is None:
        return None, None
    return context.to_carrier(), context.request_id
