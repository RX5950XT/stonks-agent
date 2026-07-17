from __future__ import annotations

from collections.abc import Callable
from typing import cast

import pytest

from stonks_agent.application.telemetry import record_operation
from stonks_agent.domain.errors import Result, Success
from stonks_agent.domain.telemetry import ComponentName, OperationName, TraceContext


class BrokenRecorder:
    def __init__(self, *, before_call: bool = False) -> None:
        self.before_call = before_call

    def record_result[T](
        self,
        *,
        component: ComponentName,
        operation: OperationName,
        call: Callable[[], Result[T]],
        parent: TraceContext | None = None,
    ) -> Result[T]:
        del component, operation, parent
        if self.before_call:
            raise RuntimeError("telemetry failed before call")
        call()
        raise RuntimeError("telemetry failed after call")


class DuplicatingRecorder:
    def record_result[T](
        self,
        *,
        component: ComponentName,
        operation: OperationName,
        call: Callable[[], Result[T]],
        parent: TraceContext | None = None,
    ) -> Result[T]:
        del component, operation, parent
        first = call()
        assert call() is first
        return first


class ForgingRecorder:
    def __init__(
        self, *, skip_call: bool = False, suppress_error: bool = False
    ) -> None:
        self.skip_call = skip_call
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
        if not self.skip_call:
            try:
                call()
            except BaseException:
                if not self.suppress_error:
                    raise
        return cast(Result[T], Success("forged"))


@pytest.mark.parametrize("before_call", (False, True))
def test_broken_recorder_never_replays_or_changes_canonical_result(
    before_call: bool,
) -> None:
    calls = 0
    expected = Success("canonical")

    def canonical() -> Result[str]:
        nonlocal calls
        calls += 1
        return expected

    result = record_operation(
        BrokenRecorder(before_call=before_call),
        component=ComponentName.EXECUTION,
        operation=OperationName.EXECUTE,
        call=canonical,
    )

    assert result is expected
    assert calls == 1


def test_duplicating_recorder_cannot_replay_canonical_call() -> None:
    calls = 0
    expected = Success("canonical")

    def canonical() -> Result[str]:
        nonlocal calls
        calls += 1
        return expected

    result = record_operation(
        DuplicatingRecorder(),
        component=ComponentName.EXECUTION,
        operation=OperationName.EXECUTE,
        call=canonical,
    )

    assert result is expected
    assert calls == 1


@pytest.mark.parametrize("skip_call", (False, True))
def test_recorder_cannot_skip_or_replace_canonical_result(skip_call: bool) -> None:
    calls = 0
    expected = Success("canonical")

    def canonical() -> Result[str]:
        nonlocal calls
        calls += 1
        return expected

    result = record_operation(
        ForgingRecorder(skip_call=skip_call),
        component=ComponentName.EXECUTION,
        operation=OperationName.EXECUTE,
        call=canonical,
    )

    assert result is expected
    assert calls == 1


def test_canonical_exception_is_reraised_once() -> None:
    calls = 0
    expected = LookupError("canonical")

    def canonical() -> Result[str]:
        nonlocal calls
        calls += 1
        raise expected

    with pytest.raises(LookupError) as raised:
        record_operation(
            BrokenRecorder(),
            component=ComponentName.EXECUTION,
            operation=OperationName.EXECUTE,
            call=canonical,
        )

    assert raised.value is expected
    assert calls == 1


def test_recorder_cannot_suppress_canonical_exception() -> None:
    calls = 0
    expected = LookupError("canonical")

    def canonical() -> Result[str]:
        nonlocal calls
        calls += 1
        raise expected

    with pytest.raises(LookupError) as raised:
        record_operation(
            ForgingRecorder(suppress_error=True),
            component=ComponentName.EXECUTION,
            operation=OperationName.EXECUTE,
            call=canonical,
        )

    assert raised.value is expected
    assert calls == 1
