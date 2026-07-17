"""Best-effort synchronous operation instrumentation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from time import monotonic
from typing import Literal

from stonks_agent.adapters.observability.context import current_trace_context
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
)
from stonks_agent.domain.telemetry import (
    ComponentName,
    MetricName,
    OperationName,
    OperationStatus,
    TraceContext,
)
from stonks_agent.ports.telemetry import MetricsPort, SpanPort, TracerPort

RuntimeEnvironment = Literal[
    "local",
    "development",
    "test",
    "staging",
    "production",
]

_DENIED_CODES = frozenset(
    {
        ErrorCode.UNAUTHORIZED,
        ErrorCode.FORBIDDEN,
        ErrorCode.CAPABILITY_DENIED,
        ErrorCode.EGRESS_DENIED,
    }
)


class OperationRecorder:
    """Instrument a typed Result call without gaining outcome authority."""

    __slots__ = ("_clock", "_environment", "_metrics", "_tracer")

    def __init__(
        self,
        *,
        metrics: MetricsPort,
        tracer: TracerPort,
        environment: RuntimeEnvironment,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._metrics = metrics
        self._tracer = tracer
        self._environment = environment
        self._clock = clock

    def record_result[T](
        self,
        *,
        component: ComponentName,
        operation: OperationName,
        call: Callable[[], Result[T]],
        parent: TraceContext | None = None,
    ) -> Result[T]:
        started = _safe_clock(self._clock)
        span = self._start_span(component, operation, parent)
        try:
            result = call()
        except BaseException:
            with suppress(BaseException):
                self._finish(
                    component=component,
                    operation=operation,
                    status=OperationStatus.ERROR,
                    error=_safe_internal_error(),
                    started=started,
                    span=span,
                )
            raise
        error = result.error if isinstance(result, Failure) else None
        status = _status(error.code) if error is not None else OperationStatus.SUCCESS
        self._finish(
            component=component,
            operation=operation,
            status=status,
            error=error,
            started=started,
            span=span,
        )
        return result

    async def record_async_result[T](
        self,
        *,
        component: ComponentName,
        operation: OperationName,
        call: Callable[[], Awaitable[Result[T]]],
        parent: TraceContext | None = None,
    ) -> Result[T]:
        started = _safe_clock(self._clock)
        span = self._start_span(component, operation, parent)
        try:
            result = await call()
        except BaseException:
            with suppress(BaseException):
                self._finish(
                    component=component,
                    operation=operation,
                    status=OperationStatus.ERROR,
                    error=_safe_internal_error(),
                    started=started,
                    span=span,
                )
            raise
        error = result.error if isinstance(result, Failure) else None
        status = _status(error.code) if error is not None else OperationStatus.SUCCESS
        self._finish(
            component=component,
            operation=operation,
            status=status,
            error=error,
            started=started,
            span=span,
        )
        return result

    def _start_span(
        self,
        component: ComponentName,
        operation: OperationName,
        parent: TraceContext | None,
    ) -> SpanPort | None:
        try:
            selected_parent = parent or current_trace_context()
            return self._tracer.start_span(
                (
                    "stonks.api.request"
                    if component is ComponentName.API
                    else "stonks.operation"
                ),
                parent=selected_parent,
                attributes={
                    "component": component,
                    "operation": operation,
                    "environment": self._environment,
                },
            )
        except Exception:
            return None

    def _finish(
        self,
        *,
        component: ComponentName,
        operation: OperationName,
        status: OperationStatus,
        error: StructuredError | None,
        started: float | None,
        span: SpanPort | None,
    ) -> None:
        attributes = _metric_attributes(
            component,
            operation,
            status,
            self._environment,
        )
        self._increment(MetricName.OPERATION_CALLS, attributes)
        if component is ComponentName.API:
            self._increment(MetricName.API_REQUESTS, attributes)
        if error is not None:
            self._increment(MetricName.OPERATION_ERRORS, attributes)
        self._observe_duration(started, attributes)
        _finish_span(span, status, error)

    def _increment(
        self,
        name: MetricName,
        attributes: Mapping[str, str],
    ) -> None:
        try:
            self._metrics.increment(name, attributes=attributes)
        except Exception:
            return

    def _observe_duration(
        self,
        started: float | None,
        attributes: Mapping[str, str],
    ) -> None:
        ended = _safe_clock(self._clock)
        duration = (
            max(0.0, ended - started)
            if started is not None and ended is not None
            else 0.0
        )
        try:
            self._metrics.observe(
                MetricName.OPERATION_DURATION,
                duration,
                attributes=attributes,
            )
        except Exception:
            return


def _metric_attributes(
    component: ComponentName,
    operation: OperationName,
    status: OperationStatus,
    environment: RuntimeEnvironment,
) -> dict[str, str]:
    return {
        "component": component,
        "operation": operation,
        "status": status,
        "environment": environment,
    }


def _finish_span(
    span: SpanPort | None,
    status: OperationStatus,
    error: StructuredError | None,
) -> None:
    if span is None:
        return
    try:
        span.set_attribute("status", status)
        if error is not None:
            span.set_attribute("error_code", error.code)
            span.record_error(error)
    except Exception:
        pass
    with suppress(Exception):
        span.end()


def _status(code: ErrorCode) -> OperationStatus:
    if code in _DENIED_CODES:
        return OperationStatus.DENIED
    if code is ErrorCode.CONFLICT:
        return OperationStatus.CONFLICT
    if code is ErrorCode.DEADLINE_EXCEEDED:
        return OperationStatus.TIMEOUT
    return OperationStatus.ERROR


def _safe_clock(clock: Callable[[], float]) -> float | None:
    try:
        return float(clock())
    except Exception:
        return None


def _safe_internal_error() -> StructuredError:
    return StructuredError(
        code=ErrorCode.INTERNAL_ERROR,
        message="Operation failed",
    )
