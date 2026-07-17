from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from stonks_agent.config.slo import (
    BreachAction,
    IndicatorKind,
    SLOPolicy,
    SLOPolicyLoadError,
    load_slo_policy,
)
from stonks_agent.domain.telemetry import MetricName

ROOT = Path(__file__).resolve().parents[2]
SLO_PATH = ROOT / "config" / "slo.yaml"
DOC_PATH = ROOT / "docs" / "operations" / "slo.md"

CORRECTNESS_IDS = (
    "duplicate_paper_order",
    "future_evidence",
    "claim_provenance",
    "risk_replayability",
)
OBJECTIVE_IDS = (
    *CORRECTNESS_IDS,
    "api_availability",
    "paper_cycle_availability",
    "api_request_latency",
    "worker_process_latency",
    "research_latency_budget",
    "paper_cycle_latency_budget",
    "research_cost_budget",
    "paper_cycle_cost_budget",
)
METRIC_NAMES = (
    "stonks_correctness_violations_total",
    "stonks_api_requests_total",
    "stonks_operation_calls_total",
    "stonks_operation_duration_seconds",
    "stonks_budget_usage_ratio",
    "stonks_budget_outcomes_total",
)


def _payload() -> dict[str, object]:
    payload = yaml.safe_load(SLO_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_versioned_slo_policy_is_complete_and_fail_closed() -> None:
    policy = load_slo_policy(SLO_PATH)

    assert policy.schema_version == 1
    assert policy.policy_id == "stonks-slo/1"
    assert policy.execution_mode == "paper"
    assert tuple(metric.name for metric in policy.metrics) == METRIC_NAMES
    assert set(METRIC_NAMES) <= {metric.value for metric in MetricName}
    assert tuple(objective.objective_id for objective in policy.objectives) == (
        OBJECTIVE_IDS
    )
    assert all(
        objective.missing_data.value == "breach" for objective in policy.objectives
    )

    correctness = policy.objectives[:4]
    assert all(
        objective.indicator.kind is IndicatorKind.VIOLATION_COUNT
        for objective in correctness
    )
    assert all(objective.target.value == "0" for objective in correctness)
    assert all(objective.error_budget.value == "0" for objective in correctness)
    assert all(
        objective.breach.action is BreachAction.FAILED for objective in correctness
    )
    assert all(objective.breach.hold_seconds == 0 for objective in correctness)

    guard = policy.budget_breach_behavior
    assert guard.execution_mode == "paper"
    assert guard.preserve_observed_commit is True
    assert guard.degraded.allow_new_target is False
    assert guard.degraded.allow_new_reservation is False
    assert guard.degraded.allow_new_order is False
    assert guard.failed.allow_new_target is False
    assert guard.failed.allow_new_reservation is False
    assert guard.failed.allow_new_order is False
    assert guard.allow_order_chasing is False
    assert guard.allow_compensating_quantity is False

    routing = policy.operator_routing
    assert routing.state.value == "policy_only"
    assert routing.paging_backend.value == "none"
    assert routing.delivery_guarantee.value == "none"
    assert {route.severity.value for route in routing.routes} == {
        "critical",
        "warning",
    }
    assert all(route.configured is False for route in routing.routes)

    limitations = policy.monitoring_limitations
    assert limitations.topology.value == "single_host"
    assert limitations.prometheus_storage.value == "ephemeral"
    assert limitations.paging_backend.value == "none"
    assert limitations.trace_storage.value == "none"


def test_slo_policy_models_are_immutable() -> None:
    policy = load_slo_policy(SLO_PATH)

    with pytest.raises(ValidationError):
        policy.policy_id = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        policy.objectives[0].target.value = "1"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (
            lambda payload: payload.update({"unknown": True}),
            "Extra inputs are not permitted",
        ),
        (
            lambda payload: payload["objectives"].pop(0),
            "at least 12 items",
        ),
        (
            lambda payload: payload["metrics"][1].update(
                {"name": payload["metrics"][0]["name"]}
            ),
            "metric names must be unique",
        ),
        (
            lambda payload: payload["objectives"][0]["target"].update({"value": "NaN"}),
            "finite canonical decimal",
        ),
        (
            lambda payload: payload["objectives"][0]["breach"].update(
                {"action": "continue"}
            ),
            "Input should be 'degraded' or 'failed'",
        ),
        (
            lambda payload: payload["metrics"][0]["labels"].append("account_id"),
            "metric catalog drifted",
        ),
        (
            lambda payload: payload["objectives"][0]["indicator"]["filters"].append(
                {"name": "order_id", "value": "raw-order"}
            ),
            "indicator filters must match the metric label catalog",
        ),
        (
            lambda payload: payload["objectives"][5]["target"].update({"value": "1.1"}),
            "ratio target must be between zero and one",
        ),
    ),
)
def test_slo_schema_rejects_policy_drift(
    mutate: object,
    message: str,
) -> None:
    payload = _payload()
    assert callable(mutate)
    mutate(payload)

    with pytest.raises(ValidationError, match=message):
        SLOPolicy.model_validate(payload)


def test_slo_loader_returns_bounded_structured_error(tmp_path: Path) -> None:
    path = tmp_path / "secret-token-do-not-copy.yaml"
    path.write_text("schema_version: [", encoding="utf-8")

    with pytest.raises(SLOPolicyLoadError) as raised:
        load_slo_policy(path)

    error = raised.value.error
    assert error.code.value == "configuration_invalid"
    assert error.message == "SLO policy configuration is invalid"
    assert error.details == {"file": "secret-token-do-not-copy.yaml"}
    assert "[" not in str(error)


def test_slo_runbook_is_honest_and_operational() -> None:
    text = DOC_PATH.read_text(encoding="utf-8")
    policy = load_slo_policy(SLO_PATH)

    for phrase in (
        "zero duplicate paper order",
        "zero future evidence",
        "100% claim provenance",
        "100% replayable risk decision",
        "error budget",
        "degraded",
        "failed",
        "不追單",
        "paper operator",
        "單機",
        "非持久",
        "尚未接上 paging backend",
        "Prometheus",
    ):
        assert phrase in text
    for heading in (
        "# SLO、預算與告警操作",
        "## Correctness SLO",
        "## Availability、latency 與 cost",
        "## Error-budget burn policy",
        "## Operator 處置",
        "## 目前限制",
    ):
        assert heading in text
    for objective in policy.objectives:
        assert f"### {objective.breach.runbook_anchor}" in text
