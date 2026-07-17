from __future__ import annotations

from asyncio import CancelledError
from collections.abc import Callable, Mapping

import pytest

from stonks_agent.adapters.observability.context import trace_scope
from stonks_agent.adapters.observability.operation import OperationRecorder
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    StructuredError,
    Success,
)
from stonks_agent.domain.telemetry import (
    ComponentName,
    MetricName,
    OperationName,
    OperationStatus,
    TraceContext,
)
from stonks_agent.ports.telemetry import OperationRecorderPort


class Span:
    def __init__(self, *, explode: bool = False) -> None:
        self.attributes: dict[str, object] = {}
        self.errors: list[StructuredError] = []
        self.ended = 0
        self.explode = explode

    def set_attribute(self, name: str, value: str | int | float | bool) -> None:
        if self.explode:
            raise RuntimeError("export failed")
        self.attributes[name] = value

    def record_error(self, error: StructuredError) -> None:
        if self.explode:
            raise RuntimeError("export failed")
        self.errors.append(error)

    def end(self) -> None:
        self.ended += 1
        if self.explode:
            raise RuntimeError("export failed")


class Tracer:
    def __init__(self, span: Span, *, explode: bool = False) -> None:
        self.span = span
        self.explode = explode
        self.parents: list[object] = []

    def start_span(
        self,
        name: str,
        *,
        parent: object = None,
        attributes: Mapping[str, str] | None = None,
    ) -> Span:
        del name, attributes
        self.parents.append(parent)
        if self.explode:
            raise RuntimeError("export failed")
        return self.span


class Metrics:
    def __init__(self, *, explode: bool = False) -> None:
        self.calls: list[tuple[str, float, dict[str, str]]] = []
        self.explode = explode

    def increment(
        self,
        name: str,
        value: int = 1,
        *,
        attributes: Mapping[str, str] | None = None,
    ) -> None:
        self._record(name, value, attributes)

    def observe(
        self,
        name: str,
        value: float,
        *,
        attributes: Mapping[str, str] | None = None,
    ) -> None:
        self._record(name, value, attributes)

    def _record(
        self,
        name: str,
        value: float,
        attributes: Mapping[str, str] | None,
    ) -> None:
        if self.explode:
            raise RuntimeError("export failed")
        self.calls.append((name, value, dict(attributes or {})))


def clock(*values: float) -> Callable[[], float]:
    remaining = iter(values)
    return lambda: next(remaining)


def test_operation_recorder_preserves_success_and_emits_bounded_signals() -> None:
    metrics = Metrics()
    span = Span()
    recorder = OperationRecorder(
        metrics=metrics,
        tracer=Tracer(span),
        environment="test",
        clock=clock(10.0, 10.25),
    )

    result = recorder.record_result(
        component=ComponentName.EXECUTION,
        operation=OperationName.EXECUTE,
        call=lambda: Success("receipt"),
    )

    assert result == Success("receipt")
    assert [item[0] for item in metrics.calls] == [
        MetricName.OPERATION_CALLS,
        MetricName.OPERATION_DURATION,
    ]
    assert all(
        item[2]
        == {
            "component": "execution",
            "operation": "execute",
            "status": "success",
            "environment": "test",
        }
        for item in metrics.calls
    )
    assert metrics.calls[1][1] == 0.25
    assert span.attributes == {"status": OperationStatus.SUCCESS}
    assert span.ended == 1


def test_operation_recorder_preserves_failure_and_records_only_safe_code() -> None:
    metrics = Metrics()
    span = Span()
    failure = Failure(
        StructuredError(
            code=ErrorCode.FORBIDDEN,
            message="Not allowed",
            details={"unsafe": "must not be telemetry"},
        )
    )
    recorder = OperationRecorder(
        metrics=metrics,
        tracer=Tracer(span),
        environment="production",
        clock=clock(1.0, 1.5),
    )

    result = recorder.record_result(
        component=ComponentName.RISK,
        operation=OperationName.AUTHORIZE,
        call=lambda: failure,
    )

    assert result is failure
    assert [item[0] for item in metrics.calls] == [
        MetricName.OPERATION_CALLS,
        MetricName.OPERATION_ERRORS,
        MetricName.OPERATION_DURATION,
    ]
    assert all(item[2]["status"] == "denied" for item in metrics.calls)
    assert span.errors == [failure.error]
    assert span.attributes == {
        "status": OperationStatus.DENIED,
        "error_code": ErrorCode.FORBIDDEN,
    }


def test_telemetry_failures_never_change_result_or_original_exception() -> None:
    recorder = OperationRecorder(
        metrics=Metrics(explode=True),
        tracer=Tracer(Span(explode=True), explode=True),
        environment="test",
        clock=lambda: (_ for _ in ()).throw(RuntimeError("clock failed")),
    )
    success = Success("canonical")

    assert isinstance(recorder, OperationRecorderPort)
    assert (
        recorder.record_result(
            component=ComponentName.DELIVERY,
            operation=OperationName.DELIVER,
            call=lambda: success,
        )
        is success
    )

    original = LookupError("canonical failure")

    def explode() -> Success[str]:
        raise original

    with pytest.raises(LookupError) as raised:
        recorder.record_result(
            component=ComponentName.DELIVERY,
            operation=OperationName.DELIVER,
            call=explode,
        )
    assert raised.value is original


def test_operation_recorder_inherits_current_context_without_adapter_imports() -> None:
    tracer = Tracer(Span())
    recorder = OperationRecorder(
        metrics=Metrics(),
        tracer=tracer,
        environment="test",
        clock=clock(1.0, 1.1),
    )
    context = TraceContext(trace_id="1" * 32, span_id="2" * 16)

    with trace_scope(context):
        result = recorder.record_result(
            component=ComponentName.SIGNAL,
            operation=OperationName.DERIVE,
            call=lambda: Success("signal"),
        )

    assert result == Success("signal")
    assert tracer.parents == [context]


@pytest.mark.asyncio
async def test_async_operation_records_actual_awaited_duration() -> None:
    metrics = Metrics()
    recorder = OperationRecorder(
        metrics=metrics,
        tracer=Tracer(Span()),
        environment="test",
        clock=clock(4.0, 4.75),
    )

    async def invoke() -> Success[str]:
        return Success("response")

    result = await recorder.record_async_result(
        component=ComponentName.API,
        operation=OperationName.HTTP_REQUEST,
        call=invoke,
    )

    assert result == Success("response")
    assert [item[0] for item in metrics.calls] == [
        MetricName.OPERATION_CALLS,
        MetricName.API_REQUESTS,
        MetricName.OPERATION_DURATION,
    ]
    assert metrics.calls[-1][1] == 0.75


@pytest.mark.asyncio
async def test_async_operation_preserves_original_exception_when_telemetry_fails() -> (
    None
):
    recorder = OperationRecorder(
        metrics=Metrics(explode=True),
        tracer=Tracer(Span(explode=True), explode=True),
        environment="test",
        clock=lambda: (_ for _ in ()).throw(RuntimeError("clock failed")),
    )
    original = RuntimeError("canonical async failure")

    async def invoke() -> Success[str]:
        raise original

    with pytest.raises(RuntimeError) as raised:
        await recorder.record_async_result(
            component=ComponentName.API,
            operation=OperationName.HTTP_REQUEST,
            call=invoke,
        )

    assert raised.value is original


@pytest.mark.asyncio
async def test_async_cancellation_ends_span_without_replacing_cancellation() -> None:
    metrics = Metrics()
    span = Span()
    recorder = OperationRecorder(
        metrics=metrics,
        tracer=Tracer(span),
        environment="test",
        clock=clock(1.0, 1.1),
    )
    original = CancelledError("canonical cancellation")

    async def invoke() -> Success[str]:
        raise original

    with pytest.raises(CancelledError) as raised:
        await recorder.record_async_result(
            component=ComponentName.WORKER,
            operation=OperationName.PROCESS,
            call=invoke,
        )

    assert raised.value is original
    assert span.ended == 1
    assert [item[0] for item in metrics.calls] == [
        MetricName.OPERATION_CALLS,
        MetricName.OPERATION_ERRORS,
        MetricName.OPERATION_DURATION,
    ]
