"""Bounded OpenTelemetry SDK adapters and OTLP/HTTP composition."""

from __future__ import annotations

import ipaddress
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Protocol, Self, cast
from urllib.parse import urlsplit

from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider, TraceBasedExemplarFilter
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    Status,
    StatusCode,
    TraceFlags,
    TraceState,
    set_span_in_context,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from requests import Response, Session

from stonks_agent.domain.errors import StructuredError
from stonks_agent.domain.telemetry import (
    METRIC_CATALOG,
    MetricKind,
    MetricName,
    TraceContext,
    validate_metric_measurement,
    validate_span_attributes,
)
from stonks_agent.ports.telemetry import SpanPort

RuntimeEnvironment = Literal[
    "local",
    "development",
    "test",
    "staging",
    "production",
]
_SPAN_NAMES = frozenset({"stonks.api.request", "stonks.operation"})
_EXPORTER_HEADERS = {"user-agent": "stonks-agent-otlp"}
_IMPLICIT_OTLP_ENVIRONMENT = frozenset(
    {
        "OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED",
        "OTEL_SDK_DISABLED",
    }
)


class OTLPHTTPConfig(BaseModel):
    """Secret-free, exact OTLP/HTTP exporter configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    endpoint: str = Field(min_length=12, max_length=512)
    service_name: Literal["stonks-agent"] = "stonks-agent"
    environment: RuntimeEnvironment
    export_interval_millis: int = Field(default=10_000, ge=1_000, le=300_000)
    export_timeout_millis: int = Field(default=5_000, ge=100, le=60_000)

    @model_validator(mode="after")
    def validate_endpoint(self) -> Self:
        parsed = urlsplit(self.endpoint)
        try:
            port = parsed.port
        except ValueError as error:
            raise ValueError("OTLP endpoint port is invalid") from error
        if (
            not self.endpoint.isascii()
            or any(character.isspace() for character in self.endpoint)
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or port is None
            or not 1 <= port <= 65_535
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or self.endpoint.endswith("/")
            or not _exact_authority(parsed.netloc, parsed.hostname, port)
        ):
            raise ValueError("OTLP endpoint must be an exact origin")
        deployed = self.environment in {"staging", "production"}
        if deployed and parsed.scheme != "https":
            raise ValueError("deployed OTLP endpoint requires HTTPS")
        if deployed and _is_ip_literal(parsed.hostname):
            raise ValueError("deployed OTLP endpoint requires a DNS identity")
        if (
            (not deployed)
            and parsed.scheme == "http"
            and not _is_loopback(parsed.hostname)
        ):
            raise ValueError("local plaintext OTLP endpoint must be loopback")
        return self

    @property
    def traces_endpoint(self) -> str:
        return f"{self.endpoint}/v1/traces"

    @property
    def metrics_endpoint(self) -> str:
        return f"{self.endpoint}/v1/metrics"


class _Counter(Protocol):
    def add(self, amount: int, attributes: Mapping[str, str]) -> None: ...


class _Histogram(Protocol):
    def record(self, amount: float, attributes: Mapping[str, str]) -> None: ...


class _Meter(Protocol):
    def create_counter(self, name: str, *, unit: str) -> _Counter: ...

    def create_histogram(self, name: str, *, unit: str) -> _Histogram: ...


class OpenTelemetryMetrics:
    """Catalog-validating OTel meter; SDK failures are best effort."""

    __slots__ = ("_counters", "_histograms")

    def __init__(self, meter: _Meter) -> None:
        self._counters = {
            name: meter.create_counter(name, unit=spec.unit)
            for name, spec in METRIC_CATALOG.items()
            if spec.kind is MetricKind.COUNTER
        }
        self._histograms = {
            name: meter.create_histogram(name, unit=spec.unit)
            for name, spec in METRIC_CATALOG.items()
            if spec.kind is MetricKind.HISTOGRAM
        }

    def increment(
        self,
        name: str,
        value: int = 1,
        *,
        attributes: Mapping[str, str] | None = None,
    ) -> None:
        validated = validate_metric_measurement(
            name,
            MetricKind.COUNTER,
            value,
            attributes or {},
        )
        selected = MetricName(name)
        try:
            self._counters[selected].add(value, validated)
        except Exception:
            return

    def observe(
        self,
        name: str,
        value: float,
        *,
        attributes: Mapping[str, str] | None = None,
    ) -> None:
        validated = validate_metric_measurement(
            name,
            MetricKind.HISTOGRAM,
            value,
            attributes or {},
        )
        selected = MetricName(name)
        try:
            self._histograms[selected].record(value, validated)
        except Exception:
            return


class _SDKSpan(Protocol):
    def set_attribute(self, name: str, value: object) -> object: ...

    def set_status(self, status: Status) -> object: ...

    def end(self) -> None: ...


class _Tracer(Protocol):
    def start_span(
        self,
        name: str,
        *,
        context: object,
        attributes: Mapping[str, str],
    ) -> _SDKSpan: ...


class OpenTelemetrySpan:
    __slots__ = ("_ended", "_span")

    def __init__(self, span: _SDKSpan) -> None:
        self._span = span
        self._ended = False

    def set_attribute(self, name: str, value: str | int | float | bool) -> None:
        validated = validate_span_attributes({name: value})
        try:
            self._span.set_attribute(name, validated[name])
        except Exception:
            return

    def record_error(self, error: StructuredError) -> None:
        validated = validate_span_attributes({"error_code": error.code})
        try:
            self._span.set_attribute("error_code", validated["error_code"])
            self._span.set_status(Status(StatusCode.ERROR))
        except Exception:
            return

    def end(self) -> None:
        if self._ended:
            return
        self._ended = True
        try:
            self._span.end()
        except Exception:
            return


class _NoOpSpan:
    __slots__ = ()

    def set_attribute(self, name: str, value: str | int | float | bool) -> None:
        validate_span_attributes({name: value})

    def record_error(self, error: StructuredError) -> None:
        validate_span_attributes({"error_code": error.code})

    def end(self) -> None:
        return None


class OpenTelemetryTracer:
    """Allowlisted OTel tracer that records no exception text or identity labels."""

    __slots__ = ("_tracer",)

    def __init__(self, tracer: _Tracer) -> None:
        self._tracer = tracer

    def start_span(
        self,
        name: str,
        *,
        parent: TraceContext | None = None,
        attributes: Mapping[str, str] | None = None,
    ) -> SpanPort:
        if name not in _SPAN_NAMES:
            raise ValueError("span name is not allowlisted")
        validated = validate_span_attributes(attributes)
        context = _otel_parent(parent)
        try:
            span = self._tracer.start_span(
                name,
                context=context,
                attributes=validated,
            )
        except Exception:
            return _NoOpSpan()
        return OpenTelemetrySpan(span)


class _Provider(Protocol):
    def force_flush(self, timeout_millis: int = 10_000) -> bool: ...

    def shutdown(self) -> None: ...


class OpenTelemetryRuntime:
    """Own both SDK providers and expose bounded best-effort lifecycle."""

    __slots__ = ("_metric_provider", "_trace_provider", "metrics", "tracer")

    def __init__(
        self,
        *,
        metrics: OpenTelemetryMetrics,
        tracer: OpenTelemetryTracer,
        trace_provider: _Provider,
        metric_provider: _Provider,
    ) -> None:
        self.metrics = metrics
        self.tracer = tracer
        self._trace_provider = trace_provider
        self._metric_provider = metric_provider

    def force_flush(self, timeout_millis: int = 10_000) -> bool:
        if not 1 <= timeout_millis <= 60_000:
            raise ValueError("telemetry flush timeout is invalid")
        trace_ok = _flush(self._trace_provider, timeout_millis)
        metric_ok = _flush(self._metric_provider, timeout_millis)
        return trace_ok and metric_ok

    def shutdown(self) -> None:
        _shutdown(self._trace_provider)
        _shutdown(self._metric_provider)


class NoOpMetrics:
    __slots__ = ()

    def increment(
        self,
        name: str,
        value: int = 1,
        *,
        attributes: Mapping[str, str] | None = None,
    ) -> None:
        validate_metric_measurement(
            name,
            MetricKind.COUNTER,
            value,
            attributes or {},
        )

    def observe(
        self,
        name: str,
        value: float,
        *,
        attributes: Mapping[str, str] | None = None,
    ) -> None:
        validate_metric_measurement(
            name,
            MetricKind.HISTOGRAM,
            value,
            attributes or {},
        )


class NoOpTracer:
    __slots__ = ()

    def start_span(
        self,
        name: str,
        *,
        parent: TraceContext | None = None,
        attributes: Mapping[str, str] | None = None,
    ) -> SpanPort:
        del parent
        if name not in _SPAN_NAMES:
            raise ValueError("span name is not allowlisted")
        validate_span_attributes(attributes)
        return _NoOpSpan()


class NoOpTelemetryRuntime:
    __slots__ = ("metrics", "tracer")

    def __init__(self) -> None:
        self.metrics = NoOpMetrics()
        self.tracer = NoOpTracer()

    def force_flush(self, timeout_millis: int = 10_000) -> bool:
        if not 1 <= timeout_millis <= 60_000:
            raise ValueError("telemetry flush timeout is invalid")
        return True

    def shutdown(self) -> None:
        return None


class _StrictOTLPSession:
    """Requests session that ignores ambient auth/proxies and never redirects."""

    __slots__ = ("_session",)

    def __init__(self) -> None:
        self._session = Session()
        self._session.trust_env = False

    @property
    def headers(self) -> Any:
        return self._session.headers

    @property
    def trust_env(self) -> bool:
        return self._session.trust_env

    def request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> Response:
        kwargs["allow_redirects"] = False
        return self._session.request(method, url, **kwargs)

    def post(
        self,
        url: str,
        data: Any = None,
        json: Any = None,
        **kwargs: Any,
    ) -> Response:
        return self.request(
            "POST",
            url,
            data=data,
            json=json,
            **kwargs,
        )

    def close(self) -> None:
        self._session.close()


def build_otlp_runtime(
    config: OTLPHTTPConfig,
) -> OpenTelemetryRuntime | NoOpTelemetryRuntime:
    if not config.enabled:
        return NoOpTelemetryRuntime()
    _reject_implicit_otlp_environment()
    resource = Resource(
        {
            "service.name": config.service_name,
            "deployment.environment.name": config.environment,
        }
    )
    trace_exporter = OTLPSpanExporter(
        endpoint=config.traces_endpoint,
        headers=_EXPORTER_HEADERS,
        timeout=config.export_timeout_millis / 1000,
        compression=Compression.NoCompression,
        session=cast(Session, _StrictOTLPSession()),
    )
    trace_provider = TracerProvider(
        sampler=ParentBased(ALWAYS_ON),
        resource=resource,
        shutdown_on_exit=False,
        span_limits=SpanLimits(
            max_attributes=8,
            max_events=0,
            max_links=0,
            max_span_attributes=8,
            max_event_attributes=0,
            max_link_attributes=0,
            max_attribute_length=128,
            max_span_attribute_length=128,
        ),
    )
    trace_provider.add_span_processor(
        BatchSpanProcessor(
            trace_exporter,
            max_queue_size=2_048,
            schedule_delay_millis=config.export_interval_millis,
            max_export_batch_size=512,
            export_timeout_millis=config.export_timeout_millis,
        )
    )
    metric_exporter = OTLPMetricExporter(
        endpoint=config.metrics_endpoint,
        headers=_EXPORTER_HEADERS,
        timeout=config.export_timeout_millis / 1000,
        compression=Compression.NoCompression,
        session=cast(Session, _StrictOTLPSession()),
    )
    metric_reader = PeriodicExportingMetricReader(
        metric_exporter,
        export_interval_millis=config.export_interval_millis,
        export_timeout_millis=config.export_timeout_millis,
    )
    metric_provider = MeterProvider(
        metric_readers=(metric_reader,),
        resource=resource,
        exemplar_filter=TraceBasedExemplarFilter(),
        shutdown_on_exit=False,
    )
    return OpenTelemetryRuntime(
        metrics=OpenTelemetryMetrics(
            cast(_Meter, metric_provider.get_meter(config.service_name))
        ),
        tracer=OpenTelemetryTracer(
            cast(_Tracer, trace_provider.get_tracer(config.service_name))
        ),
        trace_provider=trace_provider,
        metric_provider=metric_provider,
    )


def load_otlp_http_config(path: str | Path) -> OTLPHTTPConfig:
    try:
        payload = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        return OTLPHTTPConfig.model_validate(payload)
    except (
        OSError,
        TypeError,
        ValidationError,
        tomllib.TOMLDecodeError,
    ) as error:
        raise ValueError("OTLP configuration could not be loaded") from error


def _otel_parent(parent: TraceContext | None) -> object:
    if parent is None:
        return None
    trace_state = (
        TraceState()
        if parent.tracestate is None
        else TraceState.from_header([parent.tracestate])
    )
    span_context = SpanContext(
        trace_id=int(parent.trace_id, 16),
        span_id=int(parent.span_id, 16),
        is_remote=True,
        trace_flags=TraceFlags(int(parent.trace_flags, 16)),
        trace_state=trace_state,
    )
    return set_span_in_context(NonRecordingSpan(span_context))


def _flush(provider: _Provider, timeout_millis: int) -> bool:
    try:
        return provider.force_flush(timeout_millis) is not False
    except Exception:
        return False


def _shutdown(provider: _Provider) -> None:
    try:
        provider.shutdown()
    except Exception:
        return


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _exact_authority(netloc: str, host: str, port: int) -> bool:
    encoded_host = f"[{host}]" if ":" in host else host
    return netloc == f"{encoded_host}:{port}"


def _reject_implicit_otlp_environment() -> None:
    if any(
        name in _IMPLICIT_OTLP_ENVIRONMENT
        or name.startswith("OTEL_EXPORTER_OTLP")
        or name.startswith("OTEL_PYTHON_EXPORTER_OTLP_HTTP")
        for name in os.environ
    ):
        raise ValueError("implicit OTLP environment overrides are forbidden")
