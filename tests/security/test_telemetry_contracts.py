from __future__ import annotations

from collections.abc import Mapping

import pytest
from pydantic import ValidationError

from stonks_agent.ports.telemetry import MetricsPort, TraceContext


class RecordingMetrics:
    def increment(
        self,
        name: str,
        value: int = 1,
        *,
        attributes: Mapping[str, str] | None = None,
    ) -> None:
        del name, value, attributes

    def observe(
        self,
        name: str,
        value: float,
        *,
        attributes: Mapping[str, str] | None = None,
    ) -> None:
        del name, value, attributes


def test_metrics_port_is_a_runtime_checkable_protocol() -> None:
    assert isinstance(RecordingMetrics(), MetricsPort)


def test_trace_context_is_strict_and_structured() -> None:
    context = TraceContext(
        trace_id="a" * 32,
        span_id="b" * 16,
        request_id="request-1",
        run_id="run-1",
    )

    assert context.trace_id == "a" * 32
    assert context.correlation_attributes() == {
        "trace_id": "a" * 32,
        "span_id": "b" * 16,
        "request_id": "request-1",
        "run_id": "run-1",
    }


def test_trace_context_rejects_invalid_ids_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        TraceContext.model_validate(
            {"trace_id": "short", "span_id": "b" * 16, "debug": True}
        )
