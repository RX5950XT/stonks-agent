from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from stonks_agent.config.capacity import CapacityPolicyLoadError, load_capacity_policy

POLICY_PATH = Path("config/capacity.yaml")
WORKLOADS = ("api", "queue", "snapshot", "research", "forecast", "paper_cycle")
PROCESS_BUDGETS = (
    "core",
    "postgres",
    "tradingagents",
    "kronos_cpu",
    "kronos_cuda",
    "quant_lab",
)


def _payload() -> dict[str, object]:
    loaded = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _write_policy(tmp_path: Path, payload: dict[str, object]) -> Path:
    target = tmp_path / "capacity.yaml"
    target.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return target


def test_loads_frozen_closed_paper_capacity_policy() -> None:
    policy = load_capacity_policy(POLICY_PATH)

    assert policy.policy_id == "stonks-capacity/1"
    assert policy.execution_mode == "paper"
    assert policy.measurement.duration_unit == "microseconds"
    assert policy.measurement.percentile == 95
    assert policy.measurement.percentile_method == "nearest_rank"
    assert policy.measurement.production_sla_claim is False
    assert policy.resource_observation_scope == "probe_process"
    assert policy.scope.evidence_class == "single_host_ci_baseline"
    assert policy.process_budget_contract == "static_manifest_only"
    assert (
        policy.probe_runtime_budget.cpu_millicores_ceiling,
        policy.probe_runtime_budget.ram_mebibytes_ceiling,
        policy.probe_runtime_budget.pid_ceiling,
        policy.probe_runtime_budget.process_ceiling,
        policy.probe_runtime_budget.in_flight_ceiling,
    ) == (4000, 2048, 1, 1, 16)
    assert tuple(item.workload.value for item in policy.workloads) == WORKLOADS
    assert all(
        policy.measurement.minimum_sample_count
        <= item.sample_count
        <= policy.measurement.maximum_sample_count
        for item in policy.workloads
    )
    assert all(
        1 <= item.concurrency <= policy.measurement.maximum_concurrency
        for item in policy.workloads
    )
    with pytest.raises(Exception, match="frozen"):
        policy.execution_mode = "live"  # type: ignore[misc]


def test_metric_bindings_match_existing_telemetry_catalog() -> None:
    policy = load_capacity_policy(POLICY_PATH)

    assert all(
        item.metric.name == "stonks_operation_duration_seconds"
        for item in policy.workloads
    )


def test_process_budgets_are_closed_and_match_runtime_envelopes() -> None:
    policy = load_capacity_policy(POLICY_PATH)

    assert tuple(item.process_id.value for item in policy.process_budgets) == (
        PROCESS_BUDGETS
    )
    assert tuple(item.ram_mebibytes_ceiling for item in policy.process_budgets) == (
        256,
        512,
        4096,
        8192,
        12288,
        2048,
    )
    assert tuple(item.cpu_millicores_ceiling for item in policy.process_budgets) == (
        500,
        1000,
        2000,
        4000,
        4000,
        2000,
    )
    assert tuple(item.process_ceiling for item in policy.process_budgets) == (
        1,
        64,
        1,
        1,
        1,
        1,
    )
    assert tuple(item.in_flight_ceiling for item in policy.process_budgets) == (
        128,
        16,
        1,
        1,
        1,
        1,
    )
    assert all(
        item.process_ceiling <= item.pid_ceiling for item in policy.process_budgets
    )
    assert policy.process_budgets[4].gpu_vram_enforced is False
    assert all(
        item.gpu_vram_enforced is None
        for index, item in enumerate(policy.process_budgets)
        if index != 4
    )
    assert all(not hasattr(item, "process_budget_id") for item in policy.workloads)
    assert all(
        tuple(label.name for label in item.metric.labels)
        == ("component", "operation", "status", "environment")
        for item in policy.workloads
    )


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("unknown", "Extra inputs are not permitted"),
        ("missing_workload", "at least 6 items"),
        ("duplicate_workload", "catalog is incomplete, duplicated, or reordered"),
        ("sample_underflow", "sample_count is outside measurement bounds"),
        ("sample_overflow", "sample_count is outside measurement bounds"),
        ("concurrency_overflow", "concurrency is outside measurement bounds"),
        ("wall_overflow", "workload wall ceiling exceeds the bounded run"),
        ("sla_claim", "Input should be False"),
        ("live_scope", "Input should be 'local', 'development' or 'test'"),
        ("unsafe_resources", "process ceiling cannot exceed PID ceiling"),
        ("missing_process", "at least 6 items"),
        ("duplicate_process", "process budget catalog is incomplete"),
        ("cuda_vram_claim", "Input should be False"),
        ("resource_scope_drift", "Input should be 'probe_process'"),
        ("evidence_class_drift", "Input should be 'single_host_ci_baseline'"),
        ("probe_budget_drift", "probe runtime budget drifted"),
        ("manifest_claim_drift", "Input should be 'static_manifest_only'"),
        ("process_limit_drift", "process budget limits drifted"),
        ("workload_limit_drift", "capacity workload limits drifted"),
        ("measurement_drift", "capacity measurement contract drifted"),
        ("unknown_label", "telemetry attributes are not allowlisted"),
        ("unknown_label_value", "telemetry attribute value is not allowlisted"),
    ),
)
def test_loader_rejects_unknown_missing_duplicate_or_unsafe_policy(
    tmp_path: Path, mutation: str, expected: str
) -> None:
    payload = deepcopy(_payload())
    workloads = payload["workloads"]
    assert isinstance(workloads, list)
    measurement = payload["measurement"]
    assert isinstance(measurement, dict)
    if mutation == "unknown":
        payload["unreviewed"] = True
    elif mutation == "missing_workload":
        workloads.pop()
    elif mutation == "duplicate_workload":
        workloads[-1] = deepcopy(workloads[0])
    elif mutation == "sample_underflow":
        workloads[0]["sample_count"] = measurement["minimum_sample_count"] - 1
    elif mutation == "sample_overflow":
        workloads[0]["sample_count"] = measurement["maximum_sample_count"] + 1
    elif mutation == "concurrency_overflow":
        workloads[0]["concurrency"] = measurement["maximum_concurrency"] + 1
    elif mutation == "wall_overflow":
        workloads[0]["wall_ceiling_microseconds"] = (
            measurement["wall_clock_ceiling_microseconds"] + 1
        )
    elif mutation == "sla_claim":
        measurement["production_sla_claim"] = True
    elif mutation == "live_scope":
        payload["scope"]["environment"] = "production"
    elif mutation == "unsafe_resources":
        payload["process_budgets"][0]["process_ceiling"] = (
            payload["process_budgets"][0]["pid_ceiling"] + 1
        )
    elif mutation == "missing_process":
        payload["process_budgets"].pop()
    elif mutation == "duplicate_process":
        payload["process_budgets"][-1] = deepcopy(payload["process_budgets"][0])
    elif mutation == "cuda_vram_claim":
        payload["process_budgets"][4]["gpu_vram_enforced"] = True
    elif mutation == "resource_scope_drift":
        payload["resource_observation_scope"] = "host"
    elif mutation == "evidence_class_drift":
        payload["scope"]["evidence_class"] = "developer_laptop"
    elif mutation == "probe_budget_drift":
        payload["probe_runtime_budget"]["cpu_millicores_ceiling"] += 1
    elif mutation == "manifest_claim_drift":
        payload["process_budget_contract"] = "runtime_measured"
    elif mutation == "process_limit_drift":
        payload["process_budgets"][0]["cpu_millicores_ceiling"] += 1
    elif mutation == "workload_limit_drift":
        workloads[0]["p95_ceiling_microseconds"] += 1
    elif mutation == "measurement_drift":
        measurement["maximum_sample_count"] -= 1
    elif mutation == "unknown_label":
        workloads[0]["metric"]["labels"].append(
            {"name": "account_id", "value": "raw_account"}
        )
    else:
        workloads[0]["metric"]["labels"][0]["value"] = "unknown_component"

    with pytest.raises(CapacityPolicyLoadError) as raised:
        load_capacity_policy(_write_policy(tmp_path, payload))

    assert raised.value.error.code.value == "configuration_invalid"
    assert raised.value.error.details == {"file": "capacity.yaml"}
    assert expected in str(raised.value.__cause__)


def test_loader_does_not_disclose_invalid_file_contents(tmp_path: Path) -> None:
    target = tmp_path / "capacity-private.yaml"
    target.write_text("schema_version: [sensitive-marker", encoding="utf-8")

    with pytest.raises(CapacityPolicyLoadError) as raised:
        load_capacity_policy(target)

    assert raised.value.error.message == "Capacity policy configuration is invalid"
    assert raised.value.error.details == {"file": "capacity-private.yaml"}
    assert "sensitive-marker" not in str(raised.value)
