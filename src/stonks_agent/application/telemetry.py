"""Clean application helper for optional best-effort operation telemetry."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress

from stonks_agent.domain.errors import Result
from stonks_agent.domain.telemetry import ComponentName, OperationName
from stonks_agent.ports.telemetry import OperationRecorderPort


def record_operation[T](
    telemetry: OperationRecorderPort | None,
    *,
    component: ComponentName,
    operation: OperationName,
    call: Callable[[], Result[T]],
) -> Result[T]:
    if telemetry is None:
        return call()
    executed = False
    captured: Result[T] | None = None
    raised: BaseException | None = None

    def invoke() -> Result[T]:
        nonlocal captured, executed, raised
        if executed:
            if raised is not None:
                raise raised
            if captured is None:
                raise RuntimeError("Telemetry recorder lost canonical result")
            return captured
        executed = True
        try:
            captured = call()
            return captured
        except BaseException as error:
            raised = error
            raise

    with suppress(Exception):
        telemetry.record_result(
            component=component,
            operation=operation,
            call=invoke,
        )
    return invoke()
