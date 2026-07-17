from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from unittest.mock import patch

import pytest
from requests import Session

from stonks_agent.adapters.observability import otel
from stonks_agent.adapters.observability.otel import (
    OpenTelemetryMetrics,
    OpenTelemetryRuntime,
    OpenTelemetryTracer,
    OTLPHTTPConfig,
    _StrictOTLPSession,
    build_otlp_runtime,
    load_otlp_http_config,
)
from stonks_agent.domain.errors import ErrorCode, StructuredError
from stonks_agent.domain.telemetry import (
    MetricName,
    TraceContext,
)
from stonks_agent.ports.telemetry import (
    MetricsPort,
    TelemetryLifecyclePort,
    TracerPort,
)


class Instrument:
    def __init__(self, *, explode: bool = False) -> None:
        self.values: list[tuple[float, dict[str, str]]] = []
        self.explode = explode

    def add(self, value: int, attributes: Mapping[str, str]) -> None:
        self._record(value, attributes)

    def record(self, value: float, attributes: Mapping[str, str]) -> None:
        self._record(value, attributes)

    def _record(self, value: float, attributes: Mapping[str, str]) -> None:
        if self.explode:
            raise RuntimeError("export failed")
        self.values.append((value, dict(attributes)))


class Meter:
    def __init__(self, *, explode: bool = False) -> None:
        self.instruments: dict[str, Instrument] = {}
        self.explode = explode

    def create_counter(self, name: str, *, unit: str) -> Instrument:
        del unit
        instrument = Instrument(explode=self.explode)
        self.instruments[name] = instrument
        return instrument

    def create_histogram(self, name: str, *, unit: str) -> Instrument:
        del unit
        instrument = Instrument(explode=self.explode)
        self.instruments[name] = instrument
        return instrument


class OTelSpan:
    def __init__(self, *, explode: bool = False) -> None:
        self.attributes: dict[str, object] = {}
        self.status: object = None
        self.ended = 0
        self.explode = explode

    def set_attribute(self, name: str, value: object) -> None:
        if self.explode:
            raise RuntimeError("export failed")
        self.attributes[name] = value

    def set_status(self, status: object) -> None:
        if self.explode:
            raise RuntimeError("export failed")
        self.status = status

    def end(self) -> None:
        self.ended += 1
        if self.explode:
            raise RuntimeError("export failed")


class Tracer:
    def __init__(self, *, explode: bool = False) -> None:
        self.span = OTelSpan(explode=explode)
        self.calls: list[tuple[str, dict[str, str], object]] = []

    def start_span(
        self,
        name: str,
        *,
        context: object,
        attributes: Mapping[str, str],
    ) -> OTelSpan:
        self.calls.append((name, dict(attributes), context))
        return self.span


LABELS = {
    "component": "execution",
    "operation": "execute",
    "status": "success",
    "environment": "test",
}


def test_metrics_adapter_uses_catalog_and_swallows_sdk_export_failure() -> None:
    meter = Meter()
    metrics = OpenTelemetryMetrics(meter)  # type: ignore[arg-type]

    metrics.increment(
        MetricName.OPERATION_CALLS,
        attributes=LABELS,
    )
    metrics.observe(
        MetricName.OPERATION_DURATION,
        0.5,
        attributes=LABELS,
    )

    assert isinstance(metrics, MetricsPort)
    assert meter.instruments[MetricName.OPERATION_CALLS].values == [(1, LABELS)]
    assert meter.instruments[MetricName.OPERATION_DURATION].values == [(0.5, LABELS)]

    failing = OpenTelemetryMetrics(Meter(explode=True))  # type: ignore[arg-type]
    failing.increment(MetricName.OPERATION_CALLS, attributes=LABELS)
    with pytest.raises(ValueError):
        metrics.increment(
            MetricName.OPERATION_CALLS,
            attributes={**LABELS, "account_id": "account-1"},
        )


def test_tracer_adapter_propagates_parent_and_never_records_error_text() -> None:
    tracer = Tracer()
    adapter = OpenTelemetryTracer(tracer)  # type: ignore[arg-type]
    parent = TraceContext(trace_id="1" * 32, span_id="2" * 16, trace_flags="01")

    span = adapter.start_span(
        "stonks.operation",
        parent=parent,
        attributes={
            "component": "risk",
            "operation": "authorize",
            "environment": "test",
        },
    )
    span.set_attribute("status", "denied")
    span.record_error(
        StructuredError(
            code=ErrorCode.FORBIDDEN,
            message="sensitive exception text",
            details={"prompt": "sensitive"},
        )
    )
    span.end()
    span.end()

    assert isinstance(adapter, TracerPort)
    assert tracer.calls[0][0] == "stonks.operation"
    assert tracer.calls[0][2] is not None
    assert tracer.span.attributes == {
        "status": "denied",
        "error_code": "forbidden",
    }
    assert "sensitive" not in repr(tracer.span.attributes)
    assert tracer.span.ended == 1


def test_tracing_sdk_failure_is_best_effort_but_unsafe_attributes_raise() -> None:
    tracer = Tracer(explode=True)
    span = OpenTelemetryTracer(tracer).start_span(  # type: ignore[arg-type]
        "stonks.operation",
        attributes={"component": "worker", "operation": "process"},
    )
    span.set_attribute("status", "success")
    span.record_error(StructuredError(ErrorCode.INTERNAL_ERROR, "safe"))
    span.end()

    with pytest.raises(ValueError):
        OpenTelemetryTracer(Tracer()).start_span(  # type: ignore[arg-type]
            "stonks.operation",
            attributes={"url": "https://metadata.internal"},
        )


class Provider:
    def __init__(self, *, result: bool = True, explode: bool = False) -> None:
        self.result = result
        self.explode = explode
        self.flushes = 0
        self.shutdowns = 0

    def force_flush(self, timeout_millis: int) -> bool:
        assert 1 <= timeout_millis <= 60_000
        self.flushes += 1
        if self.explode:
            raise RuntimeError("export failed")
        return self.result

    def shutdown(self) -> None:
        self.shutdowns += 1
        if self.explode:
            raise RuntimeError("export failed")


def test_runtime_flush_and_shutdown_are_bounded_best_effort() -> None:
    trace = Provider()
    metrics = Provider(result=False)
    runtime = OpenTelemetryRuntime(
        metrics=OpenTelemetryMetrics(Meter()),  # type: ignore[arg-type]
        tracer=OpenTelemetryTracer(Tracer()),  # type: ignore[arg-type]
        trace_provider=trace,  # type: ignore[arg-type]
        metric_provider=metrics,  # type: ignore[arg-type]
    )

    assert isinstance(runtime, TelemetryLifecyclePort)
    assert not runtime.force_flush(500)
    runtime.shutdown()
    assert trace.flushes == metrics.flushes == 1
    assert trace.shutdowns == metrics.shutdowns == 1

    failing = OpenTelemetryRuntime(
        metrics=OpenTelemetryMetrics(Meter()),  # type: ignore[arg-type]
        tracer=OpenTelemetryTracer(Tracer()),  # type: ignore[arg-type]
        trace_provider=Provider(explode=True),  # type: ignore[arg-type]
        metric_provider=Provider(explode=True),  # type: ignore[arg-type]
    )
    assert not failing.force_flush()
    failing.shutdown()


@pytest.mark.parametrize(
    "values",
    [
        {"endpoint": "http://collector.example.test:4318"},
        {"endpoint": "https://user@collector.example.test:4318"},
        {"endpoint": "https://collector.example.test:4318?token=secret"},
        {"endpoint": "https://127.0.0.1:4318"},
        {"endpoint": "https://collector.example.test:0"},
        {"endpoint": "https://collector.example.test:04318"},
        {"endpoint": "https://COLLECTOR.example.test:4318"},
        {"endpoint": " https://collector.example.test:4318"},
        {"endpoint": "https://collector.example.test:4318\n"},
        {"endpoint": "https://collector.example.test\t:4318"},
        {"endpoint": "https://coll\rector.example.test:4318"},
        {"service_name": "user-123"},
        {"export_interval_millis": 1},
    ],
)
def test_otlp_config_rejects_unsafe_production_values(
    values: dict[str, object],
) -> None:
    defaults: dict[str, object] = {
        "enabled": True,
        "endpoint": "https://collector.example.test:4318",
        "service_name": "stonks-agent",
        "environment": "production",
    }
    with pytest.raises(ValueError):
        OTLPHTTPConfig.model_validate({**defaults, **values})


def test_otlp_config_derives_exact_http_export_paths() -> None:
    config = OTLPHTTPConfig(
        enabled=True,
        endpoint="https://collector.example.test:4318",
        service_name="stonks-agent",
        environment="production",
    )

    assert config.traces_endpoint == "https://collector.example.test:4318/v1/traces"
    assert config.metrics_endpoint == "https://collector.example.test:4318/v1/metrics"


def test_default_config_loads_disabled_noop_runtime() -> None:
    config = load_otlp_http_config(Path("config/observability/default.toml"))
    runtime = build_otlp_runtime(config)

    assert not config.enabled
    assert runtime.force_flush()
    runtime.shutdown()


def test_otlp_session_ignores_ambient_credentials_proxies_and_redirects() -> None:
    session = _StrictOTLPSession()

    assert not session.trust_env
    with (
        patch.object(Session, "request", return_value=object()) as request,
        patch.object(Session, "close") as close,
    ):
        response = session.request(
            "POST",
            "https://collector.example.test:4318/v1/traces",
            allow_redirects=True,
        )
        session.close()

    assert response is request.return_value
    assert request.call_args.kwargs["allow_redirects"] is False
    close.assert_called_once_with()


@pytest.mark.parametrize(
    "name",
    [
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE",
        "OTEL_PYTHON_EXPORTER_OTLP_HTTP_TRACES_CREDENTIAL_PROVIDER",
        "OTEL_SDK_DISABLED",
        "OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED",
    ],
)
def test_enabled_runtime_rejects_implicit_otel_environment(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    secret = "ambient-secret-value"
    monkeypatch.setenv(name, secret)
    config = OTLPHTTPConfig(
        enabled=True,
        endpoint="https://collector.example.test:4318",
        environment="production",
    )

    with pytest.raises(ValueError) as raised:
        build_otlp_runtime(config)

    assert str(raised.value) == "implicit OTLP environment overrides are forbidden"
    assert secret not in str(raised.value)


def test_runtime_uses_exact_resource_and_bounded_batch_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "OTEL_RESOURCE_ATTRIBUTES",
        "credential=ambient-secret-value,account.id=user-1",
    )
    monkeypatch.setenv("OTEL_BSP_MAX_QUEUE_SIZE", "invalid")
    captured: dict[str, object] = {}

    class Processor:
        def __init__(self, exporter: object, **kwargs: object) -> None:
            captured["exporter"] = exporter
            captured["batch"] = kwargs

    monkeypatch.setattr(otel, "BatchSpanProcessor", Processor)
    config = OTLPHTTPConfig(
        enabled=True,
        endpoint="https://collector.example.test:4318",
        environment="production",
    )

    runtime = build_otlp_runtime(config)
    resource = runtime._trace_provider.resource  # type: ignore[union-attr]

    assert resource.attributes == {
        "service.name": "stonks-agent",
        "deployment.environment.name": "production",
    }
    assert captured["batch"] == {
        "max_queue_size": 2_048,
        "schedule_delay_millis": 10_000,
        "max_export_batch_size": 512,
        "export_timeout_millis": 5_000,
    }
    runtime.shutdown()


def test_config_loader_returns_generic_failure(tmp_path: Path) -> None:
    path = tmp_path / "invalid.toml"
    path.write_text("enabled = [", encoding="utf-8")

    with pytest.raises(ValueError, match="could not be loaded"):
        load_otlp_http_config(path)
