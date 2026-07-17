from __future__ import annotations

import logging

from stonks_agent.adapters.observability.context import trace_scope
from stonks_agent.adapters.observability.logging import RedactingFormatter
from stonks_agent.domain.redaction import REDACTED
from stonks_agent.domain.telemetry import TraceContext


def test_formatter_adds_current_correlation_then_redacts_complete_output() -> None:
    formatter = RedactingFormatter(
        "%(trace_id)s %(span_id)s %(request_id)s %(message)s",
        known_secrets=("runtime-sensitive-value",),
    )
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="credential=runtime-sensitive-value",
        args=(),
        exc_info=None,
    )
    context = TraceContext(
        trace_id="1" * 32,
        span_id="2" * 16,
        request_id="request-1",
    )

    with trace_scope(context):
        rendered = formatter.format(record)

    assert "1" * 32 in rendered
    assert "2" * 16 in rendered
    assert "request-1" in rendered
    assert "runtime-sensitive-value" not in rendered
    assert REDACTED in rendered


def test_formatter_supplies_bounded_placeholders_without_context() -> None:
    formatter = RedactingFormatter(
        "%(trace_id)s %(span_id)s %(request_id)s %(run_id)s %(job_id)s %(message)s"
    )
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="safe",
        args=(),
        exc_info=None,
    )

    assert formatter.format(record) == "- - - - - safe"


def test_formatter_escapes_control_characters_that_can_forge_log_records() -> None:
    formatter = RedactingFormatter("%(levelname)s %(message)s")
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="safe\nERROR forged\r\x1b[31m",
        args=(),
        exc_info=None,
    )

    rendered = formatter.format(record)

    assert rendered == r"INFO safe\u000aERROR forged\u000d\u001b[31m"
    assert "\n" not in rendered
    assert "\r" not in rendered
    assert "\x1b" not in rendered
