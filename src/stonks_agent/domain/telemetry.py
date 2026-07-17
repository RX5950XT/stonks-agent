"""Immutable W3C tracing and low-cardinality telemetry contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stonks_agent.domain.errors import ErrorCode
from stonks_agent.domain.redaction import redact_text

_TRACEPARENT = re.compile(
    r"^00-(?P<trace_id>[0-9a-f]{32})-(?P<span_id>[0-9a-f]{16})-"
    r"(?P<trace_flags>00|01)$"
)
_CORRELATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_TRACESTATE_KEY = re.compile(
    r"^(?:[a-z0-9][_0-9a-z*/-]{0,255}|"
    r"[a-z0-9][_0-9a-z*/-]{0,240}@[a-z0-9][a-z0-9*/-]{0,13})$"
)
_SENSITIVE_TRACESTATE_KEYS = frozenset(
    {
        "access_token",
        "api-key",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "client_secret",
        "cookie",
        "credential",
        "id_token",
        "password",
        "refresh_token",
        "secret",
        "token",
    }
)
_ENVIRONMENTS = frozenset({"local", "development", "test", "staging", "production"})


class TraceCarrier(BaseModel):
    """Exact W3C trace-context headers safe for durable propagation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    traceparent: str = Field(min_length=55, max_length=55)
    tracestate: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def validate_w3c_context(self) -> Self:
        match = _TRACEPARENT.fullmatch(self.traceparent)
        if match is None:
            raise ValueError("traceparent is invalid")
        if set(match.group("trace_id")) == {"0"}:
            raise ValueError("trace identifier cannot be zero")
        if set(match.group("span_id")) == {"0"}:
            raise ValueError("span identifier cannot be zero")
        if self.tracestate is not None:
            _validate_tracestate(self.tracestate)
        return self

    @classmethod
    def parse(cls, headers: Mapping[str, str]) -> TraceCarrier:
        normalized: dict[str, str] = {}
        for key, value in headers.items():
            if not isinstance(key, str):
                raise ValueError("trace context header name must be text")
            lowered = key.lower()
            if lowered not in {"traceparent", "tracestate"}:
                continue
            if lowered in normalized:
                raise ValueError("trace context header is ambiguous")
            if not isinstance(value, str):
                raise ValueError("trace context header must be text")
            normalized[lowered] = value
        traceparent = normalized.get("traceparent")
        if traceparent is None:
            raise ValueError("traceparent header is required")
        return cls(
            traceparent=traceparent,
            tracestate=normalized.get("tracestate"),
        )

    def to_headers(self) -> dict[str, str]:
        headers = {"traceparent": self.traceparent}
        if self.tracestate is not None:
            headers["tracestate"] = self.tracestate
        return headers

    @property
    def trace_id(self) -> str:
        return self.traceparent[3:35]

    @property
    def parent_span_id(self) -> str:
        return self.traceparent[36:52]

    @property
    def trace_flags(self) -> str:
        return self.traceparent[53:55]

    @property
    def sampled(self) -> bool:
        return self.trace_flags == "01"


class CorrelationContext(BaseModel):
    """Opaque correlation identifiers excluded from metric/span labels."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    run_id: str | None = Field(default=None, min_length=1, max_length=128)
    job_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("request_id", "run_id", "job_id")
    @classmethod
    def validate_identifier(cls, value: str | None) -> str | None:
        if value is not None and _CORRELATION_ID.fullmatch(value) is None:
            raise ValueError("correlation identifier is invalid")
        return value


class TraceContext(BaseModel):
    """Current span identity plus separately bounded log correlation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    span_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    trace_flags: str = Field(default="00", pattern=r"^(?:00|01)$")
    tracestate: str | None = Field(default=None, min_length=1, max_length=512)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    run_id: str | None = Field(default=None, min_length=1, max_length=128)
    job_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("trace_id", "span_id")
    @classmethod
    def reject_zero_ids(cls, value: str) -> str:
        if set(value) == {"0"}:
            raise ValueError("trace identifiers cannot be all zero")
        return value

    @field_validator("request_id", "run_id", "job_id")
    @classmethod
    def validate_correlation_id(cls, value: str | None) -> str | None:
        return CorrelationContext.validate_identifier(value)

    @field_validator("tracestate")
    @classmethod
    def validate_tracestate(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_tracestate(value)
        return value

    @classmethod
    def from_carrier(
        cls,
        carrier: TraceCarrier,
        *,
        correlation: CorrelationContext | None = None,
    ) -> TraceContext:
        selected = correlation or CorrelationContext()
        return cls(
            trace_id=carrier.trace_id,
            span_id=carrier.parent_span_id,
            trace_flags=carrier.trace_flags,
            tracestate=carrier.tracestate,
            **selected.model_dump(),
        )

    def to_carrier(self) -> TraceCarrier:
        return TraceCarrier(
            traceparent=(f"00-{self.trace_id}-{self.span_id}-{self.trace_flags}"),
            tracestate=self.tracestate,
        )

    @property
    def correlation(self) -> CorrelationContext:
        return CorrelationContext(
            request_id=self.request_id,
            run_id=self.run_id,
            job_id=self.job_id,
        )

    def correlation_attributes(self) -> dict[str, str]:
        values: dict[str, str | None] = {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            **self.correlation.model_dump(),
        }
        return {key: value for key, value in values.items() if value is not None}


class ComponentName(StrEnum):
    API = "api"
    PROVIDER = "provider"
    QUEUE = "queue"
    WORKER = "worker"
    LLM = "llm"
    MODEL = "model"
    SIGNAL = "signal"
    RISK = "risk"
    EXECUTION = "execution"
    RECONCILIATION = "reconciliation"
    DELIVERY = "delivery"


class OperationName(StrEnum):
    HTTP_REQUEST = "http_request"
    FETCH = "fetch"
    ENQUEUE = "enqueue"
    CLAIM = "claim"
    PROCESS = "process"
    COMPLETE = "complete"
    GENERATE = "generate"
    INFER = "infer"
    DERIVE = "derive"
    AUTHORIZE = "authorize"
    EXECUTE = "execute"
    RECONCILE = "reconcile"
    DELIVER = "deliver"


class OperationStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    DENIED = "denied"
    CONFLICT = "conflict"
    TIMEOUT = "timeout"
    RETRY = "retry"
    SKIPPED = "skipped"


class MetricKind(StrEnum):
    COUNTER = "counter"
    HISTOGRAM = "histogram"


class CorrectnessInvariant(StrEnum):
    DUPLICATE_PAPER_ORDER = "duplicate_paper_order"
    FUTURE_EVIDENCE = "future_evidence"
    CLAIM_PROVENANCE = "claim_provenance"
    RISK_REPLAYABILITY = "risk_replayability"


class BudgetDimension(StrEnum):
    COST = "cost"
    LATENCY = "latency"


class BudgetScope(StrEnum):
    RESEARCH = "research"
    PAPER_CYCLE = "paper_cycle"


class BudgetOutcome(StrEnum):
    WITHIN = "within"
    DEGRADED = "degraded"
    FAILED = "failed"


class MetricName(StrEnum):
    API_REQUESTS = "stonks_api_requests_total"
    OPERATION_CALLS = "stonks_operation_calls_total"
    OPERATION_ERRORS = "stonks_operation_errors_total"
    OPERATION_DURATION = "stonks_operation_duration_seconds"
    CORRECTNESS_VIOLATIONS = "stonks_correctness_violations_total"
    BUDGET_USAGE_RATIO = "stonks_budget_usage_ratio"
    BUDGET_OUTCOMES = "stonks_budget_outcomes_total"


class MetricSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: MetricName
    kind: MetricKind
    unit: str = Field(min_length=1, max_length=16)


METRIC_CATALOG: Mapping[MetricName, MetricSpec] = MappingProxyType(
    {
        MetricName.API_REQUESTS: MetricSpec(
            name=MetricName.API_REQUESTS,
            kind=MetricKind.COUNTER,
            unit="{request}",
        ),
        MetricName.OPERATION_CALLS: MetricSpec(
            name=MetricName.OPERATION_CALLS,
            kind=MetricKind.COUNTER,
            unit="{operation}",
        ),
        MetricName.OPERATION_ERRORS: MetricSpec(
            name=MetricName.OPERATION_ERRORS,
            kind=MetricKind.COUNTER,
            unit="{error}",
        ),
        MetricName.OPERATION_DURATION: MetricSpec(
            name=MetricName.OPERATION_DURATION,
            kind=MetricKind.HISTOGRAM,
            unit="s",
        ),
        MetricName.CORRECTNESS_VIOLATIONS: MetricSpec(
            name=MetricName.CORRECTNESS_VIOLATIONS,
            kind=MetricKind.COUNTER,
            unit="{violation}",
        ),
        MetricName.BUDGET_USAGE_RATIO: MetricSpec(
            name=MetricName.BUDGET_USAGE_RATIO,
            kind=MetricKind.HISTOGRAM,
            unit="1",
        ),
        MetricName.BUDGET_OUTCOMES: MetricSpec(
            name=MetricName.BUDGET_OUTCOMES,
            kind=MetricKind.COUNTER,
            unit="{evaluation}",
        ),
    }
)

_LABEL_VALUES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "component": frozenset(ComponentName),
        "operation": frozenset(OperationName),
        "status": frozenset(OperationStatus),
        "environment": _ENVIRONMENTS,
    }
)
_SPAN_VALUES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        **_LABEL_VALUES,
        "error_code": frozenset(ErrorCode),
    }
)
_CORRECTNESS_LABEL_VALUES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "invariant": frozenset(CorrectnessInvariant),
        "environment": _ENVIRONMENTS,
    }
)
_BUDGET_USAGE_LABEL_VALUES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "budget": frozenset(BudgetDimension),
        "scope": frozenset(BudgetScope),
        "environment": _ENVIRONMENTS,
    }
)
_BUDGET_OUTCOME_LABEL_VALUES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        **_BUDGET_USAGE_LABEL_VALUES,
        "outcome": frozenset(BudgetOutcome),
    }
)
_METRIC_LABEL_VALUES: Mapping[MetricName, Mapping[str, frozenset[str]]] = (
    MappingProxyType(
        {
            MetricName.API_REQUESTS: _LABEL_VALUES,
            MetricName.OPERATION_CALLS: _LABEL_VALUES,
            MetricName.OPERATION_ERRORS: _LABEL_VALUES,
            MetricName.OPERATION_DURATION: _LABEL_VALUES,
            MetricName.CORRECTNESS_VIOLATIONS: _CORRECTNESS_LABEL_VALUES,
            MetricName.BUDGET_USAGE_RATIO: _BUDGET_USAGE_LABEL_VALUES,
            MetricName.BUDGET_OUTCOMES: _BUDGET_OUTCOME_LABEL_VALUES,
        }
    )
)


def validate_metric_measurement(
    name: MetricName | str,
    kind: MetricKind,
    value: int | float,
    attributes: Mapping[str, Any],
) -> Mapping[str, str]:
    try:
        selected_name = MetricName(name)
    except ValueError as error:
        raise ValueError("metric is not catalogued") from error
    spec = METRIC_CATALOG[selected_name]
    if spec.kind is not kind:
        raise ValueError("metric instrument kind is invalid")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("metric value is invalid")
    if not isfinite(float(value)) or value < 0:
        raise ValueError("metric value must be finite and non-negative")
    if value > 1_000_000:
        raise ValueError("metric value exceeds the bounded catalog")
    if kind is MetricKind.COUNTER:
        minimum = 0 if selected_name is MetricName.CORRECTNESS_VIOLATIONS else 1
        if not isinstance(value, int) or value < minimum:
            raise ValueError("counter value is outside its catalogued bound")
    validated = _validate_attributes(
        attributes,
        allowed=_METRIC_LABEL_VALUES[selected_name],
        require_all=True,
    )
    if selected_name is MetricName.API_REQUESTS and (
        validated["component"] != ComponentName.API
        or validated["operation"] != OperationName.HTTP_REQUEST
    ):
        raise ValueError("API metric labels are invalid")
    return validated


def validate_span_attributes(
    attributes: Mapping[str, Any] | None,
) -> Mapping[str, str]:
    return _validate_attributes(
        attributes or {},
        allowed=_SPAN_VALUES,
        require_all=False,
    )


def _validate_attributes(
    attributes: Mapping[str, Any],
    *,
    allowed: Mapping[str, frozenset[str]],
    require_all: bool,
) -> Mapping[str, str]:
    keys = set(attributes)
    expected = set(allowed)
    if (require_all and keys != expected) or not keys <= set(allowed):
        raise ValueError("telemetry attributes are not allowlisted")
    normalized: dict[str, str] = {}
    for key, value in attributes.items():
        if not isinstance(value, str) or value not in allowed[key]:
            raise ValueError("telemetry attribute value is not allowlisted")
        normalized[key] = value
    return MappingProxyType(normalized)


def _validate_tracestate(value: str) -> None:
    if value.strip() != value or redact_text(value) != value:
        raise ValueError("tracestate is unsafe")
    entries = value.split(",")
    if not 1 <= len(entries) <= 32:
        raise ValueError("tracestate entry count is invalid")
    keys: set[str] = set()
    for entry in entries:
        key, separator, opaque = entry.partition("=")
        if (
            separator != "="
            or _TRACESTATE_KEY.fullmatch(key) is None
            or key in _SENSITIVE_TRACESTATE_KEYS
            or not opaque
            or opaque.strip() != opaque
            or any(
                ord(character) < 0x20
                or ord(character) > 0x7E
                or character in {",", "="}
                for character in opaque
            )
            or key in keys
        ):
            raise ValueError("tracestate entry is invalid")
        keys.add(key)
