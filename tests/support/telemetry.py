from __future__ import annotations

from collections.abc import Callable

from stonks_agent.domain.errors import Result
from stonks_agent.domain.telemetry import ComponentName, OperationName, TraceContext


class RecordingOperationRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[ComponentName, OperationName]] = []

    def record_result[T](
        self,
        *,
        component: ComponentName,
        operation: OperationName,
        call: Callable[[], Result[T]],
        parent: TraceContext | None = None,
    ) -> Result[T]:
        del parent
        self.calls.append((component, operation))
        return call()
