from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast

import pytest
from sqlalchemy import Engine

from stonks_agent.adapters.postgres.job_queue import PostgresJobQueue
from stonks_agent.domain.errors import Result, Success
from stonks_agent.domain.telemetry import (
    ComponentName,
    OperationName,
    TraceContext,
)


class Recorder:
    def __init__(
        self,
        *,
        fail_before: bool = False,
        fail_after: bool = False,
        invoke_twice: bool = False,
        skip_call: bool = False,
        replace_result: bool = False,
        suppress_error: bool = False,
    ) -> None:
        self.fail_before = fail_before
        self.fail_after = fail_after
        self.invoke_twice = invoke_twice
        self.skip_call = skip_call
        self.replace_result = replace_result
        self.suppress_error = suppress_error

    def record_result[T](
        self,
        *,
        component: ComponentName,
        operation: OperationName,
        call: Callable[[], Result[T]],
        parent: TraceContext | None = None,
    ) -> Result[T]:
        del component, operation, parent
        if self.fail_before:
            raise RuntimeError("exporter failed before invocation")
        if self.skip_call:
            return cast(Result[T], Success("forged"))
        try:
            result = call()
        except BaseException:
            if self.suppress_error:
                return cast(Result[T], Success("forged"))
            raise
        if self.invoke_twice:
            assert call() is result
        if self.fail_after:
            raise RuntimeError("exporter failed after invocation")
        if self.replace_result:
            return cast(Result[T], Success("forged"))
        return result

    async def record_async_result[T](
        self,
        *,
        component: ComponentName,
        operation: OperationName,
        call: Callable[[], Awaitable[Result[T]]],
        parent: TraceContext | None = None,
    ) -> Result[T]:
        del component, operation, parent
        return await call()


@pytest.mark.parametrize(
    "recorder",
    (
        Recorder(fail_before=True),
        Recorder(fail_after=True),
    ),
)
def test_recorder_failure_never_replays_successful_queue_call(
    recorder: Recorder,
) -> None:
    queue = PostgresJobQueue(cast(Engine, object()), recorder=recorder)
    calls = 0

    def canonical_call() -> Success[str]:
        nonlocal calls
        calls += 1
        return Success("canonical")

    result = queue._record(OperationName.ENQUEUE, canonical_call)

    assert result == Success("canonical")
    assert calls == 1


def test_canonical_exception_is_re_raised_exactly_once() -> None:
    queue = PostgresJobQueue(cast(Engine, object()), recorder=Recorder())
    calls = 0
    original = LookupError("canonical failure")

    def canonical_call() -> Success[str]:
        nonlocal calls
        calls += 1
        raise original

    with pytest.raises(LookupError) as raised:
        queue._record(OperationName.CLAIM, canonical_call)

    assert raised.value is original
    assert calls == 1


def test_recorder_cannot_invoke_queue_mutation_twice() -> None:
    queue = PostgresJobQueue(
        cast(Engine, object()),
        recorder=Recorder(invoke_twice=True),
    )
    calls = 0

    def canonical_call() -> Success[str]:
        nonlocal calls
        calls += 1
        return Success("canonical")

    result = queue._record(OperationName.COMPLETE, canonical_call)

    assert result == Success("canonical")
    assert calls == 1


@pytest.mark.parametrize(
    "recorder",
    (
        Recorder(skip_call=True),
        Recorder(replace_result=True),
    ),
)
def test_recorder_cannot_skip_or_replace_queue_result(recorder: Recorder) -> None:
    queue = PostgresJobQueue(cast(Engine, object()), recorder=recorder)
    calls = 0
    expected = Success("canonical")

    def canonical_call() -> Success[str]:
        nonlocal calls
        calls += 1
        return expected

    result = queue._record(OperationName.ENQUEUE, canonical_call)

    assert result is expected
    assert calls == 1


def test_recorder_cannot_suppress_queue_exception() -> None:
    queue = PostgresJobQueue(
        cast(Engine, object()),
        recorder=Recorder(suppress_error=True),
    )
    calls = 0
    original = LookupError("canonical failure")

    def canonical_call() -> Success[str]:
        nonlocal calls
        calls += 1
        raise original

    with pytest.raises(LookupError) as raised:
        queue._record(OperationName.COMPLETE, canonical_call)

    assert raised.value is original
    assert calls == 1
