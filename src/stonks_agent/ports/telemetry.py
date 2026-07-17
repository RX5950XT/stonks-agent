"""Runtime-checkable vendor-neutral tracing and metrics ports."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result, StructuredError
from stonks_agent.domain.telemetry import (
    ComponentName,
    OperationName,
    TraceContext,
)


@runtime_checkable
class MetricsPort(Protocol):
    def increment(
        self,
        name: str,
        value: int = 1,
        *,
        attributes: Mapping[str, str] | None = None,
    ) -> None: ...

    def observe(
        self,
        name: str,
        value: float,
        *,
        attributes: Mapping[str, str] | None = None,
    ) -> None: ...


@runtime_checkable
class SpanPort(Protocol):
    def set_attribute(self, name: str, value: str | int | float | bool) -> None: ...

    def record_error(self, error: StructuredError) -> None: ...

    def end(self) -> None: ...


@runtime_checkable
class TracerPort(Protocol):
    def start_span(
        self,
        name: str,
        *,
        parent: TraceContext | None = None,
        attributes: Mapping[str, str] | None = None,
    ) -> SpanPort: ...


@runtime_checkable
class TelemetryLifecyclePort(Protocol):
    def force_flush(self, timeout_millis: int = 10_000) -> bool: ...

    def shutdown(self) -> None: ...


@runtime_checkable
class OperationRecorderPort(Protocol):
    def record_result[T](
        self,
        *,
        component: ComponentName,
        operation: OperationName,
        call: Callable[[], Result[T]],
        parent: TraceContext | None = None,
    ) -> Result[T]: ...

    async def record_async_result[T](
        self,
        *,
        component: ComponentName,
        operation: OperationName,
        call: Callable[[], Awaitable[Result[T]]],
        parent: TraceContext | None = None,
    ) -> Result[T]: ...


__all__ = [
    "MetricsPort",
    "OperationRecorderPort",
    "SpanPort",
    "TelemetryLifecyclePort",
    "TraceContext",
    "TracerPort",
]
