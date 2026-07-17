from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
OBSERVABILITY = ROOT / "infra" / "observability"
PROMETHEUS_CONFIG = OBSERVABILITY / "prometheus.yaml"
COLLECTOR_CONFIG = OBSERVABILITY / "otel-collector.yaml"
COMPOSE_CONFIG = ROOT / "infra" / "compose.observability.yaml"
RULES = OBSERVABILITY / "rules"
FIXTURE = ROOT / "tests" / "fixtures" / "observability" / "prometheus_rules.test.yaml"
SOURCE_METRICS = frozenset(
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
CORRECTNESS_ALERTS = {
    "StonksDuplicatePaperOrderDetected": "duplicate_paper_order",
    "StonksFutureEvidenceDetected": "future_evidence",
    "StonksClaimProvenanceViolation": "claim_provenance",
    "StonksRiskReplayabilityViolation": "risk_replayability",
}
SERVICE_ALERTS = frozenset(
    {
        "StonksApiAvailabilityLow",
        "StonksWorkerAvailabilityLow",
        "StonksApiLatencyHigh",
        "StonksWorkerLatencyHigh",
        "StonksBudgetFastBurn",
        "StonksBudgetSlowBurn",
        "StonksBudgetExhausted",
        "StonksBudgetHardFailure",
        "StonksBudgetUsageHigh",
        "StonksBudgetSoftThresholdExceeded",
        "StonksCorrectnessTelemetryMissing",
    }
)
RECORDING_RULES = frozenset(
    {
        "stonks:slo_api_availability:ratio_5m",
        "stonks:slo_worker_availability:ratio_5m",
        "stonks:slo_api_latency_seconds:p95_5m",
        "stonks:slo_worker_latency_seconds:p95_5m",
        "stonks:budget_burn:rate_5m",
        "stonks:budget_burn:rate_1h",
        "stonks:budget_burn:rate_30d",
        "stonks:budget_usage:ratio_p95_15m",
    }
)
LOW_CARDINALITY_RULE_LABELS = frozenset(
    {"severity", "category", "service", "invariant", "route"}
)


def _load_yaml(path: Path) -> dict[str, object]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _rules(path: Path) -> list[dict[str, object]]:
    payload = _load_yaml(path)
    groups = payload["groups"]
    assert isinstance(groups, list)
    return [rule for group in groups for rule in group["rules"]]


def test_rules_cover_correctness_service_and_budget_slos() -> None:
    recording = _rules(RULES / "recording.yaml")
    alerts = _rules(RULES / "alerts.yaml")
    records = {rule["record"] for rule in recording}
    by_name = {rule["alert"]: rule for rule in alerts}

    assert records == RECORDING_RULES
    assert set(by_name) == set(CORRECTNESS_ALERTS) | SERVICE_ALERTS

    for alert, invariant in CORRECTNESS_ALERTS.items():
        rule = by_name[alert]
        expression = str(rule["expr"])
        assert rule["for"] == "0m"
        assert rule["labels"] == {
            "severity": "critical",
            "category": "correctness",
            "invariant": invariant,
            "route": "critical_paper_operator",
        }
        assert "increase(stonks_correctness_violations_total" in expression
        assert f'invariant="{invariant}"' in expression
        assert "[1m]) > 0" in expression
        assert rule["annotations"]["window"] == "1m"

    for alert in SERVICE_ALERTS - {
        "StonksBudgetExhausted",
        "StonksBudgetHardFailure",
    }:
        rule = by_name[alert]
        assert rule["for"] not in {None, "", "0m", "0s"}
        assert rule["labels"]["severity"] in {"warning", "critical"}
        assert rule["annotations"]["window"]

    exhausted = by_name["StonksBudgetExhausted"]
    assert exhausted["labels"] == {
        "severity": "critical",
        "category": "budget",
        "route": "critical_paper_operator",
    }
    assert exhausted["for"] == "0m"
    hard = by_name["StonksBudgetHardFailure"]
    assert 'outcome="failed"' in str(hard["expr"])
    assert hard["for"] == "0m"
    missing = by_name["StonksCorrectnessTelemetryMissing"]
    assert "absent_over_time(stonks_correctness_violations_total[5m])" in str(
        missing["expr"]
    )
    assert missing["for"] == "5m"


def test_rule_output_labels_are_bounded_and_contain_no_raw_identity() -> None:
    rules = _rules(RULES / "recording.yaml") + _rules(RULES / "alerts.yaml")
    serialized = json.dumps(rules, sort_keys=True)

    for rule in rules:
        assert set(rule.get("labels", {})) <= LOW_CARDINALITY_RULE_LABELS
    for forbidden in (
        "account_id",
        "correlation_id",
        "instrument_id",
        "job_id",
        "order_id",
        "request_id",
        "run_id",
        "symbol",
        "trace_id",
        "user_id",
    ):
        assert forbidden not in serialized


def test_collector_scraper_and_compose_wire_the_complete_metric_catalog() -> None:
    collector = _load_yaml(COLLECTOR_CONFIG)
    prometheus = _load_yaml(PROMETHEUS_CONFIG)
    compose = _load_yaml(COMPOSE_CONFIG)

    assert prometheus["rule_files"] == ["/etc/prometheus/rules/*.yaml"]
    scrape = prometheus["scrape_configs"][0]
    relabel = scrape["metric_relabel_configs"]
    rendered_relabel = json.dumps(relabel, sort_keys=True)
    rendered_processors = json.dumps(collector["processors"], sort_keys=True)
    for metric in SOURCE_METRICS:
        assert metric in rendered_relabel
        assert metric in rendered_processors
    for label in (
        "budget",
        "component",
        "environment",
        "invariant",
        "le",
        "operation",
        "outcome",
        "scope",
        "status",
    ):
        assert label in rendered_relabel
    datapoint = collector["processors"]["transform/allowlists"]["metric_statements"][1][
        "statements"
    ]
    rendered_datapoint = json.dumps(datapoint)
    for label in (
        "budget",
        "component",
        "environment",
        "invariant",
        "operation",
        "outcome",
        "scope",
        "status",
    ):
        assert label in rendered_datapoint

    prometheus_volumes = compose["services"]["prometheus"]["volumes"]
    assert "./observability/rules:/etc/prometheus/rules:ro" in prometheus_volumes


def test_pinned_promtool_validates_config_rules_and_alert_fixtures() -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable")
    image = _load_yaml(COMPOSE_CONFIG)["services"]["prometheus"]["image"]
    present = subprocess.run(
        [docker, "image", "inspect", image],
        check=False,
        capture_output=True,
        timeout=10,
    )
    if present.returncode != 0:
        pytest.skip("pinned Prometheus image is not present locally")

    mount = f"type=bind,src={ROOT},dst=/workspace,readonly"
    commands = (
        ("check", "config", "/workspace/infra/observability/prometheus.yaml"),
        ("check", "rules", "/workspace/infra/observability/rules/recording.yaml"),
        ("check", "rules", "/workspace/infra/observability/rules/alerts.yaml"),
        (
            "test",
            "rules",
            "/workspace/tests/fixtures/observability/prometheus_rules.test.yaml",
        ),
    )
    for arguments in commands:
        checked = subprocess.run(
            [
                docker,
                "run",
                "--rm",
                "--entrypoint",
                "/bin/promtool",
                "--mount",
                mount,
                image,
                *arguments,
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert checked.returncode == 0, checked.stdout + checked.stderr
