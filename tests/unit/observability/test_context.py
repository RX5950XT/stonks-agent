from __future__ import annotations

from stonks_agent.adapters.observability.context import (
    bind_trace_context,
    create_trace_context,
    current_trace_carrier,
    current_trace_context,
    reset_trace_context,
    trace_scope,
)
from stonks_agent.domain.telemetry import CorrelationContext, TraceCarrier

TRACE_ID = "1" * 32
PARENT_SPAN_ID = "2" * 16
CHILD_SPAN_ID = "3" * 16


class Generator:
    def new_trace_id(self) -> str:
        return TRACE_ID

    def new_span_id(self) -> str:
        return CHILD_SPAN_ID


def test_context_binding_is_nested_and_resets_exactly() -> None:
    assert current_trace_context() is None
    first = create_trace_context(
        correlation=CorrelationContext(request_id="request-1"),
        generator=Generator(),
    )
    token = bind_trace_context(first)
    assert current_trace_context() == first
    assert current_trace_carrier() == first.to_carrier()
    assert first.trace_flags == "01"

    second = create_trace_context(
        parent=TraceCarrier(
            traceparent=f"00-{TRACE_ID}-{PARENT_SPAN_ID}-01",
        ),
        correlation=CorrelationContext(request_id="request-2"),
        generator=Generator(),
    )
    with trace_scope(second):
        assert current_trace_context() == second
        assert second.trace_id == TRACE_ID
        assert second.span_id == CHILD_SPAN_ID
        assert second.trace_flags == "01"
    assert current_trace_context() == first

    reset_trace_context(token)
    assert current_trace_context() is None
    assert current_trace_carrier() is None


def test_generator_output_is_validated() -> None:
    class Invalid:
        def new_trace_id(self) -> str:
            return "0" * 32

        def new_span_id(self) -> str:
            return "0" * 16

    try:
        create_trace_context(generator=Invalid())
    except ValueError:
        pass
    else:
        raise AssertionError("zero trace identifiers accepted")


def test_generator_failure_is_normalized_without_sensitive_text() -> None:
    class Exploding:
        def new_trace_id(self) -> str:
            raise RuntimeError("sensitive generator failure")

        def new_span_id(self) -> str:
            raise RuntimeError("sensitive generator failure")

    try:
        create_trace_context(generator=Exploding())
    except ValueError as error:
        assert str(error) == "trace identifier generator returned invalid output"
        assert "sensitive" not in str(error)
    else:
        raise AssertionError("generator failure escaped normalization")
