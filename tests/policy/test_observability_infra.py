from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest
import yaml

from stonks_agent.adapters.observability.operation import OperationRecorder
from stonks_agent.adapters.observability.otel import (
    OTLPHTTPConfig,
    build_otlp_runtime,
)
from stonks_agent.application.slo_metrics import SLOMetricsRecorder
from stonks_agent.domain.errors import Success
from stonks_agent.domain.telemetry import (
    BudgetDimension,
    BudgetOutcome,
    BudgetScope,
    ComponentName,
    OperationName,
)

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "infra" / "compose.observability.yaml"
OBSERVABILITY = ROOT / "infra" / "observability"
IMAGE_LOCK_PATH = OBSERVABILITY / "images.lock.yaml"
SERVICES = frozenset({"otel-collector", "prometheus", "grafana"})
METRICS = frozenset(
    {
        "stonks_api_requests_total",
        "stonks_operation_calls_total",
        "stonks_operation_errors_total",
        "stonks_operation_duration_seconds",
        "stonks_correctness_violations_total",
        "stonks_budget_usage_ratio",
        "stonks_budget_outcomes_total",
    }
)
LABELS = frozenset(
    {
        "budget",
        "component",
        "environment",
        "invariant",
        "operation",
        "outcome",
        "scope",
        "status",
    }
)
COMPONENTS = (
    "api|provider|queue|worker|llm|model|signal|risk|execution|reconciliation|delivery"
)
OPERATIONS = (
    "http_request|fetch|enqueue|claim|process|complete|generate|infer|derive|"
    "authorize|execute|reconcile|deliver"
)
STATUSES = "success|error|denied|conflict|timeout|retry|skipped"
ENVIRONMENTS = "local|development|test|staging|production"
PINNED_IMAGE = re.compile(r"^[a-z0-9./_-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$")


def _load_yaml(path: Path) -> dict[str, object]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _compose() -> dict[str, object]:
    return _load_yaml(COMPOSE_PATH)


def test_observability_compose_is_pinned_internal_and_loopback_only() -> None:
    compose = _compose()
    services = compose["services"]
    assert isinstance(services, dict)
    assert set(services) == SERVICES
    networks = compose["networks"]
    assert isinstance(networks, dict)
    assert set(networks) == {
        "observability_ingress",
        "observability_internal",
    }
    assert networks["observability_internal"]["internal"] is True
    assert networks["observability_ingress"] == {
        "driver": "bridge",
        "driver_opts": {
            "com.docker.network.bridge.host_binding_ipv4": "127.0.0.1",
        },
    }

    for _name, service in services.items():
        assert PINNED_IMAGE.fullmatch(service["image"])
        assert ":latest" not in service["image"]
        assert service["read_only"] is True
        assert service["init"] is True
        assert service["user"].split(":", maxsplit=1)[0] not in {"0", "root"}
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["pids_limit"] >= 16
        assert service["mem_limit"]
        assert 0 < float(service["cpus"]) <= 1
        assert service["healthcheck"]["test"]
        assert service["healthcheck"]["timeout"]
        assert service["healthcheck"]["retries"] >= 3
        assert service.get("privileged") is not True
        assert service.get("network_mode") != "host"
        for volume in service.get("volumes", []):
            assert "/var/run/docker.sock" not in volume
            assert volume.endswith(":ro")
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:")

    assert services["prometheus"].get("ports", []) == []
    assert services["prometheus"]["networks"] == ["observability_internal"]
    assert services["otel-collector"]["networks"] == [
        "observability_internal",
        "observability_ingress",
    ]
    assert services["grafana"]["networks"] == [
        "observability_internal",
        "observability_ingress",
    ]
    assert services["otel-collector"]["ports"] == [
        "127.0.0.1:${STONKS_OTLP_GRPC_PORT:-4317}:4317",
        "127.0.0.1:${STONKS_OTLP_HTTP_PORT:-4318}:4318",
        "127.0.0.1:${STONKS_OTEL_HEALTH_PORT:-13133}:13133",
    ]
    assert services["grafana"]["ports"] == [
        "127.0.0.1:${STONKS_GRAFANA_PORT:-3000}:3000"
    ]
    assert any(
        entry.startswith("/var/lib/grafana:") for entry in services["grafana"]["tmpfs"]
    )


def test_observability_images_match_registry_verified_lock() -> None:
    compose = _compose()
    image_lock = _load_yaml(IMAGE_LOCK_PATH)
    images = image_lock["images"]

    assert image_lock["schema_version"] == 1
    assert image_lock["verified_at"] == "2026-07-17"
    assert image_lock["verified_by"] == "docker buildx imagetools inspect"
    assert image_lock["registry"] == "docker.io"
    assert set(images) == SERVICES

    digest = re.compile(r"^sha256:[0-9a-f]{64}$")
    for service_name, locked in images.items():
        assert digest.fullmatch(locked["manifest_list_digest"])
        assert digest.fullmatch(locked["linux_amd64_digest"])
        expected = (
            f"{locked['repository']}:{locked['tag']}@{locked['manifest_list_digest']}"
        )
        assert compose["services"][service_name]["image"] == expected


def test_grafana_requires_external_secrets_and_disables_ambient_admin() -> None:
    compose = _compose()
    grafana = compose["services"]["grafana"]
    environment = grafana["environment"]

    assert environment["GF_AUTH_ANONYMOUS_ENABLED"] == "false"
    assert environment["GF_USERS_ALLOW_SIGN_UP"] == "false"
    assert environment["GF_LIVE_MAX_CONNECTIONS"] == "0"
    assert environment["GF_NEWS_NEWS_FEED_ENABLED"] == "false"
    assert environment["GF_PLUGINS_PREINSTALL_AUTO_UPDATE"] == "false"
    assert environment["GF_PLUGINS_PREINSTALL_DISABLED"] == "true"
    assert environment["GF_SECURITY_ADMIN_USER"] != "admin"
    assert environment["GF_SECURITY_ADMIN_PASSWORD__FILE"] == (
        "/run/secrets/grafana_admin_password"
    )
    assert environment["GF_SECURITY_SECRET_KEY__FILE"] == (
        "/run/secrets/grafana_secret_key"
    )
    assert "GF_SECURITY_ADMIN_PASSWORD" not in environment
    assert grafana["secrets"] == [
        "grafana_admin_password",
        "grafana_secret_key",
    ]
    secrets = compose["secrets"]
    assert "missing-grafana-admin-password" in secrets["grafana_admin_password"]["file"]
    assert "missing-grafana-secret-key" in secrets["grafana_secret_key"]["file"]


def test_collector_has_no_log_or_host_receiver_and_enforces_catalog() -> None:
    collector = _load_yaml(OBSERVABILITY / "otel-collector.yaml")
    receivers = collector["receivers"]
    processors = collector["processors"]
    exporters = collector["exporters"]
    pipelines = collector["service"]["pipelines"]

    assert set(receivers) == {"otlp", "otlp/trace_sink"}
    assert not {"filelog", "journald", "hostmetrics", "docker_stats"} & set(receivers)
    assert "logs" not in pipelines
    assert not {"debug", "logging"} & set(exporters)
    assert pipelines["metrics"]["receivers"] == ["otlp"]
    assert pipelines["metrics"]["exporters"] == ["prometheus"]
    assert pipelines["traces"]["receivers"] == ["otlp"]
    assert pipelines["traces"]["exporters"] == ["otlp/traces"]
    assert pipelines["traces/sink"]["receivers"] == ["otlp/trace_sink"]
    assert pipelines["traces/sink"]["exporters"] == ["nop/traces"]
    assert exporters["otlp/traces"]["endpoint"] == "127.0.0.1:4319"
    assert exporters["prometheus"]["resource_to_telemetry_conversion"] == {
        "enabled": False
    }
    assert set(collector["extensions"]) == {"health_check"}
    assert collector["service"]["extensions"] == ["health_check"]
    telemetry_metrics = collector["service"]["telemetry"]["metrics"]
    assert "address" not in telemetry_metrics
    assert telemetry_metrics["readers"] == [
        {
            "pull": {
                "exporter": {
                    "prometheus": {
                        "host": "127.0.0.1",
                        "port": 8888,
                        "without_type_suffix": True,
                        "without_units": True,
                    }
                }
            }
        }
    ]
    trace_statements = processors["transform/allowlists"]["trace_statements"]
    assert any(statement["context"] == "spanevent" for statement in trace_statements)
    assert "stonks.api.request" in json.dumps(trace_statements)
    assert "stonks.operation" in json.dumps(trace_statements)

    serialized = json.dumps(processors, sort_keys=True)
    assert all(metric in serialized for metric in METRICS)
    assert all(label in serialized for label in LABELS)
    for forbidden in (
        "account_id",
        "exception.message",
        "prompt",
        "symbol",
        "url.full",
        "user.id",
    ):
        assert forbidden not in serialized


def test_prometheus_relabels_to_exact_low_cardinality_catalog() -> None:
    prometheus = _load_yaml(OBSERVABILITY / "prometheus.yaml")
    assert prometheus["global"]["scrape_interval"] == "15s"
    assert prometheus["global"]["scrape_timeout"] == "5s"
    assert "remote_write" not in prometheus
    assert prometheus["rule_files"] == ["/etc/prometheus/rules/*.yaml"]
    scrape = prometheus["scrape_configs"]
    assert len(scrape) == 1
    collector = scrape[0]
    assert collector["static_configs"] == [{"targets": ["otel-collector:8889"]}]
    assert collector["sample_limit"] <= 10_000
    assert collector["label_limit"] <= 8
    relabel = collector["metric_relabel_configs"]
    assert relabel == [
        {
            "source_labels": ["__name__"],
            "regex": (
                "stonks_api_requests_total|stonks_operation_calls_total|"
                "stonks_operation_errors_total|"
                "stonks_operation_duration_seconds_(bucket|sum|count)|"
                "stonks_correctness_violations_total|"
                "stonks_budget_usage_ratio_(bucket|sum|count)|"
                "stonks_budget_outcomes_total"
            ),
            "action": "keep",
        },
        {
            "source_labels": ["component"],
            "regex": f"^$|{COMPONENTS}",
            "action": "keep",
        },
        {
            "source_labels": ["operation"],
            "regex": f"^$|{OPERATIONS}",
            "action": "keep",
        },
        {
            "source_labels": ["status"],
            "regex": f"^$|{STATUSES}",
            "action": "keep",
        },
        {
            "source_labels": ["environment"],
            "regex": ENVIRONMENTS,
            "action": "keep",
        },
        {
            "source_labels": ["invariant"],
            "regex": (
                "^$|duplicate_paper_order|future_evidence|"
                "claim_provenance|risk_replayability"
            ),
            "action": "keep",
        },
        {
            "source_labels": ["budget"],
            "regex": "^$|cost|latency",
            "action": "keep",
        },
        {
            "source_labels": ["scope"],
            "regex": "^$|research|paper_cycle",
            "action": "keep",
        },
        {
            "source_labels": ["outcome"],
            "regex": "^$|within|degraded|failed",
            "action": "keep",
        },
        {
            "regex": (
                "__name__|budget|component|environment|invariant|le|"
                "operation|outcome|scope|status"
            ),
            "action": "labelkeep",
        },
    ]
    rendered = json.dumps(relabel, sort_keys=True)
    for forbidden in ("account", "prompt", "symbol", "user", "url"):
        assert forbidden not in rendered


def test_grafana_provisioning_is_read_only_and_uses_internal_prometheus() -> None:
    datasource = _load_yaml(
        OBSERVABILITY / "grafana" / "provisioning" / "datasources" / "prometheus.yaml"
    )
    dashboards = _load_yaml(
        OBSERVABILITY / "grafana" / "provisioning" / "dashboards" / "default.yaml"
    )
    dashboard = json.loads(
        (OBSERVABILITY / "grafana" / "dashboards" / "stonks-overview.json").read_text(
            encoding="utf-8"
        )
    )

    source = datasource["datasources"][0]
    assert source["uid"] == "stonks-prometheus"
    assert source["url"] == "http://prometheus:9090"
    assert source["access"] == "proxy"
    assert source["editable"] is False
    provider = dashboards["providers"][0]
    assert provider["allowUiUpdates"] is False
    assert provider["options"]["path"] == "/var/lib/grafana/dashboards"
    assert dashboard["editable"] is False
    assert dashboard["uid"] == "stonks-overview"
    queries = json.dumps(dashboard["panels"], sort_keys=True)
    assert "stonks_api_requests_total" in queries
    assert "stonks_operation_calls_total" in queries
    assert "stonks_operation_errors_total" in queries
    assert "stonks_operation_duration_seconds_bucket" in queries
    for forbidden in ("account_id", "prompt", "symbol", "user_id", "url"):
        assert forbidden not in queries


def test_docker_compose_observability_manifest_renders(tmp_path: Path) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable")
    password = tmp_path / "grafana-admin"
    secret_key = tmp_path / "grafana-secret-key"
    password.write_text("compose-render-only-value", encoding="utf-8")
    secret_key.write_text("compose-render-only-key-value", encoding="utf-8")
    environment = {
        **os.environ,
        "STONKS_GRAFANA_ADMIN_PASSWORD_FILE": str(password),
        "STONKS_GRAFANA_SECRET_KEY_FILE": str(secret_key),
    }

    rendered = subprocess.run(
        [docker, "compose", "-f", str(COMPOSE_PATH), "config", "--quiet"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert rendered.returncode == 0, rendered.stderr


def test_observability_stack_smoke_when_images_are_already_local(
    tmp_path: Path,
) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable")
    compose = _compose()
    images = [service["image"] for service in compose["services"].values()]
    for image in images:
        present = subprocess.run(
            [docker, "image", "inspect", image],
            check=False,
            capture_output=True,
            timeout=10,
        )
        if present.returncode != 0:
            pytest.skip("observability images are not all present locally")

    password = tmp_path / "grafana-admin"
    secret_key = tmp_path / "grafana-secret-key"
    password.write_text("bounded-smoke-admin-value", encoding="utf-8")
    secret_key.write_text("bounded-smoke-secret-key-value", encoding="utf-8")
    health_port, grafana_port, grpc_port, http_port = (
        _free_loopback_port() for _ in range(4)
    )
    project = f"stonks-observability-smoke-{os.getpid()}"
    environment = {
        **os.environ,
        "STONKS_GRAFANA_ADMIN_PASSWORD_FILE": str(password),
        "STONKS_GRAFANA_SECRET_KEY_FILE": str(secret_key),
        "STONKS_OTEL_HEALTH_PORT": str(health_port),
        "STONKS_GRAFANA_PORT": str(grafana_port),
        "STONKS_OTLP_GRPC_PORT": str(grpc_port),
        "STONKS_OTLP_HTTP_PORT": str(http_port),
    }
    command = [docker, "compose", "-p", project, "-f", str(COMPOSE_PATH)]

    try:
        started = subprocess.run(
            [*command, "up", "--detach", "--wait", "--pull", "never"],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert started.returncode == 0, started.stderr
        _wait_for_health(f"http://127.0.0.1:{health_port}/")
        _wait_for_health(f"http://127.0.0.1:{grafana_port}/api/health")
        _emit_smoke_telemetry(http_port)
        metrics = _wait_for_collector_metrics(command, environment)
        assert "stonks_api_requests_total" in metrics
        assert "stonks_operation_calls_total" in metrics
        assert "stonks_correctness_violations_total" in metrics
        assert "stonks_budget_usage_ratio_bucket" in metrics
        assert "stonks_budget_outcomes_total" in metrics
        assert 'component="api"' in metrics
        assert 'operation="http_request"' in metrics
        assert 'environment="test"' in metrics
        assert 'invariant="future_evidence"' in metrics
        assert 'budget="cost"' in metrics
        assert 'scope="research"' in metrics
        assert 'outcome="within"' in metrics
        assert "request_id" not in metrics
        scraped = _wait_for_prometheus_metrics(command, environment)
        assert "stonks_api_requests_total" in scraped
        assert "stonks_correctness_violations_total" in scraped
        assert "stonks_budget_usage_ratio_bucket" in scraped
        assert "stonks_budget_outcomes_total" in scraped
    finally:
        subprocess.run(
            [*command, "down", "--remove-orphans"],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            timeout=30,
        )


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_health(url: str) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.25)
    raise AssertionError(f"health endpoint did not become ready: {url}")


def _emit_smoke_telemetry(http_port: int) -> None:
    runtime = build_otlp_runtime(
        OTLPHTTPConfig(
            enabled=True,
            endpoint=f"http://127.0.0.1:{http_port}",
            environment="test",
            export_interval_millis=1_000,
            export_timeout_millis=5_000,
        )
    )
    try:
        recorder = OperationRecorder(
            metrics=runtime.metrics,
            tracer=runtime.tracer,
            environment="test",
        )
        result = recorder.record_result(
            component=ComponentName.API,
            operation=OperationName.HTTP_REQUEST,
            call=lambda: Success(None),
        )
        assert isinstance(result, Success)
        slo = SLOMetricsRecorder(metrics=runtime.metrics, environment="test")
        slo.record_budget_evaluation(
            budget=BudgetDimension.COST,
            scope=BudgetScope.RESEARCH,
            outcome=BudgetOutcome.WITHIN,
            usage_ratio=0.75,
        )
        assert runtime.force_flush(5_000)
    finally:
        runtime.shutdown()


def _wait_for_collector_metrics(
    command: list[str],
    environment: dict[str, str],
) -> str:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        response = subprocess.run(
            [
                *command,
                "exec",
                "-T",
                "prometheus",
                "wget",
                "-q",
                "-T",
                "3",
                "-O",
                "-",
                "http://otel-collector:8889/metrics",
            ],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if response.returncode == 0 and "stonks_api_requests_total" in response.stdout:
            return response.stdout
        time.sleep(0.25)
    raise AssertionError("collector did not expose canonical smoke metrics")


def _wait_for_prometheus_metrics(
    command: list[str],
    environment: dict[str, str],
) -> str:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        response = subprocess.run(
            [
                *command,
                "exec",
                "-T",
                "prometheus",
                "wget",
                "-q",
                "-T",
                "3",
                "-O",
                "-",
                "http://127.0.0.1:9090/api/v1/label/__name__/values",
            ],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if (
            response.returncode == 0
            and "stonks_correctness_violations_total" in response.stdout
            and "stonks_budget_outcomes_total" in response.stdout
        ):
            return response.stdout
        time.sleep(0.25)
    raise AssertionError("Prometheus did not ingest the complete metric catalog")
