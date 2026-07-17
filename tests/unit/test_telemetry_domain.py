from __future__ import annotations

from math import inf, nan

import pytest
from pydantic import ValidationError

from stonks_agent.domain.telemetry import (
    BudgetDimension,
    BudgetOutcome,
    BudgetScope,
    ComponentName,
    CorrectnessInvariant,
    CorrelationContext,
    MetricKind,
    MetricName,
    OperationName,
    OperationStatus,
    TraceCarrier,
    TraceContext,
    validate_metric_measurement,
    validate_span_attributes,
)

TRACE_ID = "1" * 32
SPAN_ID = "2" * 16


def test_trace_carrier_parses_and_formats_exact_w3c_headers() -> None:
    carrier = TraceCarrier.parse(
        {
            "TraceParent": f"00-{TRACE_ID}-{SPAN_ID}-01",
            "TraceState": "vendor=value,other=opaque",
        }
    )

    assert carrier.trace_id == TRACE_ID
    assert carrier.parent_span_id == SPAN_ID
    assert carrier.trace_flags == "01"
    assert carrier.sampled
    assert carrier.to_headers() == {
        "traceparent": f"00-{TRACE_ID}-{SPAN_ID}-01",
        "tracestate": "vendor=value,other=opaque",
    }


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"traceparent": ""},
        {"traceparent": f"ff-{TRACE_ID}-{SPAN_ID}-01"},
        {"traceparent": f"00-{'0' * 32}-{SPAN_ID}-01"},
        {"traceparent": f"00-{TRACE_ID}-{'0' * 16}-01"},
        {"traceparent": f"00-{TRACE_ID}-{SPAN_ID}-02"},
        {"traceparent": f"00-{'A' * 32}-{SPAN_ID}-01"},
        {
            "traceparent": f"00-{TRACE_ID}-{SPAN_ID}-01",
            "TraceParent": f"00-{TRACE_ID}-{SPAN_ID}-01",
        },
        {
            "traceparent": f"00-{TRACE_ID}-{SPAN_ID}-01",
            "tracestate": "vendor=one,vendor=two",
        },
        {
            "traceparent": f"00-{TRACE_ID}-{SPAN_ID}-01",
            "tracestate": "secret=Bearer token",
        },
        {
            "traceparent": f"00-{TRACE_ID}-{SPAN_ID}-01",
            "tracestate": "auth=opaque",
        },
        {
            "traceparent": f"00-{TRACE_ID}-{SPAN_ID}-01",
            "tracestate": "cookie=opaque",
        },
    ],
)
def test_trace_carrier_rejects_ambiguous_or_invalid_headers(
    headers: dict[str, str],
) -> None:
    with pytest.raises((ValueError, ValidationError)):
        TraceCarrier.parse(headers)


def test_trace_context_round_trips_carrier_and_correlation() -> None:
    carrier = TraceCarrier(
        traceparent=f"00-{TRACE_ID}-{SPAN_ID}-01",
        tracestate="vendor=value",
    )
    correlation = CorrelationContext(
        request_id="request-1",
        run_id="run-1",
        job_id="job-1",
    )

    context = TraceContext.from_carrier(carrier, correlation=correlation)

    assert context.correlation == correlation
    assert context.to_carrier() == carrier
    assert context.correlation_attributes() == {
        "trace_id": TRACE_ID,
        "span_id": SPAN_ID,
        "request_id": "request-1",
        "run_id": "run-1",
        "job_id": "job-1",
    }


@pytest.mark.parametrize(
    "identifier",
    [
        "",
        " leading",
        "trailing ",
        "contains/slash",
        "contains?query",
        "x" * 129,
    ],
)
def test_correlation_rejects_unsafe_or_unbounded_identifiers(identifier: str) -> None:
    with pytest.raises(ValidationError):
        CorrelationContext(request_id=identifier)


def test_metric_catalog_accepts_only_exact_low_cardinality_labels() -> None:
    attributes = validate_metric_measurement(
        MetricName.OPERATION_DURATION,
        MetricKind.HISTOGRAM,
        0.125,
        {
            "component": ComponentName.EXECUTION,
            "operation": OperationName.EXECUTE,
            "status": OperationStatus.SUCCESS,
            "environment": "production",
        },
    )

    assert attributes == {
        "component": "execution",
        "operation": "execute",
        "status": "success",
        "environment": "production",
    }


@pytest.mark.parametrize(
    ("name", "kind", "attributes"),
    [
        (
            MetricName.CORRECTNESS_VIOLATIONS,
            MetricKind.COUNTER,
            {
                "invariant": CorrectnessInvariant.FUTURE_EVIDENCE,
                "environment": "production",
            },
        ),
        (
            MetricName.BUDGET_USAGE_RATIO,
            MetricKind.HISTOGRAM,
            {
                "budget": BudgetDimension.COST,
                "scope": BudgetScope.RESEARCH,
                "environment": "production",
            },
        ),
        (
            MetricName.BUDGET_OUTCOMES,
            MetricKind.COUNTER,
            {
                "budget": BudgetDimension.LATENCY,
                "scope": BudgetScope.PAPER_CYCLE,
                "outcome": BudgetOutcome.FAILED,
                "environment": "production",
            },
        ),
    ],
)
def test_slo_metric_catalog_has_exact_metric_specific_labels(
    name: MetricName,
    kind: MetricKind,
    attributes: dict[str, str],
) -> None:
    assert validate_metric_measurement(name, kind, 1, attributes) == attributes


def test_correctness_counter_allows_zero_only_for_series_initialization() -> None:
    attributes = {
        "invariant": CorrectnessInvariant.RISK_REPLAYABILITY,
        "environment": "production",
    }

    assert (
        validate_metric_measurement(
            MetricName.CORRECTNESS_VIOLATIONS,
            MetricKind.COUNTER,
            0,
            attributes,
        )
        == attributes
    )
    with pytest.raises(ValueError):
        validate_metric_measurement(
            MetricName.OPERATION_CALLS,
            MetricKind.COUNTER,
            0,
            {
                "component": "worker",
                "operation": "process",
                "status": "success",
                "environment": "production",
            },
        )


@pytest.mark.parametrize(
    ("name", "kind", "attributes"),
    [
        (
            MetricName.CORRECTNESS_VIOLATIONS,
            MetricKind.COUNTER,
            {
                "invariant": "account-123",
                "environment": "production",
            },
        ),
        (
            MetricName.BUDGET_USAGE_RATIO,
            MetricKind.HISTOGRAM,
            {
                "budget": "tokens",
                "scope": "research",
                "environment": "production",
            },
        ),
        (
            MetricName.BUDGET_OUTCOMES,
            MetricKind.COUNTER,
            {
                "budget": "latency",
                "scope": "paper_cycle",
                "outcome": "failed",
                "environment": "production",
                "run_id": "run-1",
            },
        ),
    ],
)
def test_slo_metrics_reject_identity_and_unknown_dimensions(
    name: MetricName,
    kind: MetricKind,
    attributes: dict[str, str],
) -> None:
    with pytest.raises(ValueError):
        validate_metric_measurement(name, kind, 1, attributes)


def test_component_and_operation_catalog_covers_canonical_boundaries() -> None:
    assert {item.value for item in ComponentName} == {
        "api",
        "provider",
        "queue",
        "worker",
        "llm",
        "model",
        "signal",
        "risk",
        "execution",
        "reconciliation",
        "delivery",
    }
    assert {
        "http_request",
        "fetch",
        "enqueue",
        "claim",
        "process",
        "complete",
        "generate",
        "infer",
        "derive",
        "authorize",
        "execute",
        "reconcile",
        "deliver",
    } <= {item.value for item in OperationName}


@pytest.mark.parametrize(
    ("attributes", "value"),
    [
        (
            {
                "component": "execution",
                "operation": "execute",
                "status": "success",
                "environment": "production",
                "account_id": "account-1",
            },
            1.0,
        ),
        (
            {
                "component": "execution",
                "operation": "execute",
                "status": "success",
                "environment": "production",
                "symbol": "AAPL",
            },
            1.0,
        ),
        (
            {
                "component": "execution",
                "operation": "https://metadata.internal",
                "status": "success",
                "environment": "production",
            },
            1.0,
        ),
        (
            {
                "component": "execution",
                "operation": "execute",
                "status": "Bearer token",
                "environment": "production",
            },
            1.0,
        ),
        (
            {
                "component": "execution",
                "operation": "execute",
                "status": "success",
                "environment": "production",
            },
            nan,
        ),
        (
            {
                "component": "execution",
                "operation": "execute",
                "status": "success",
                "environment": "production",
            },
            inf,
        ),
        (
            {
                "component": "execution",
                "operation": "execute",
                "status": "success",
                "environment": "production",
            },
            1_000_001,
        ),
    ],
)
def test_metric_catalog_rejects_high_cardinality_or_nonfinite_values(
    attributes: dict[str, str],
    value: float,
) -> None:
    with pytest.raises(ValueError):
        validate_metric_measurement(
            MetricName.OPERATION_DURATION,
            MetricKind.HISTOGRAM,
            value,
            attributes,
        )


def test_metric_name_kind_and_required_labels_are_exact() -> None:
    labels = {
        "component": "execution",
        "operation": "execute",
        "status": "success",
        "environment": "production",
    }
    with pytest.raises(ValueError):
        validate_metric_measurement(
            "stonks_unknown_total",
            MetricKind.COUNTER,
            1,
            labels,
        )
    with pytest.raises(ValueError):
        validate_metric_measurement(
            MetricName.OPERATION_DURATION,
            MetricKind.COUNTER,
            1,
            labels,
        )
    with pytest.raises(ValueError):
        validate_metric_measurement(
            MetricName.OPERATION_DURATION,
            MetricKind.HISTOGRAM,
            1,
            {"component": "execution"},
        )


def test_span_attributes_use_the_same_exact_bounded_catalog() -> None:
    validated = validate_span_attributes(
        {
            "component": ComponentName.RISK,
            "operation": OperationName.AUTHORIZE,
            "status": OperationStatus.DENIED,
            "environment": "test",
        }
    )
    assert validated["component"] == "risk"

    for forbidden in ("user_id", "prompt", "url", "exception", "secret"):
        with pytest.raises(ValueError):
            validate_span_attributes(
                {
                    "component": "risk",
                    "operation": "authorize",
                    "status": "denied",
                    "environment": "test",
                    forbidden: "opaque",
                }
            )
