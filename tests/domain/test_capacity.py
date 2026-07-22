from __future__ import annotations

from math import nan
from pathlib import Path

import pytest
from pydantic import ValidationError

from stonks_agent.config.capacity import load_capacity_policy
from stonks_agent.domain.capacity import (
    CapacityReport,
    CapacitySample,
    CapacityVerificationError,
    CapacityWorkloadReport,
    ResourceObservation,
    verify_capacity_report,
)

POLICY = load_capacity_policy(Path("config/capacity.yaml"))


def _resources(**changes: int) -> ResourceObservation:
    values = {
        "cpu_millicores": 250,
        "ram_mebibytes": 128,
        "pid_count": 1,
        "process_count": 1,
        "in_flight_count": 1,
    }
    values.update(changes)
    return ResourceObservation(**values)


def _samples(
    *,
    count: int,
    durations: tuple[int, ...] | None = None,
    resources: ResourceObservation | None = None,
) -> tuple[CapacitySample, ...]:
    selected = durations or tuple(1_000 for _ in range(count))
    assert len(selected) == count
    cursor = 0
    result: list[CapacitySample] = []
    for index, duration in enumerate(selected):
        result.append(
            CapacitySample(
                sample_id=index + 1,
                started_at_microseconds=cursor,
                finished_at_microseconds=cursor + duration,
                resources=resources or _resources(),
            )
        )
        cursor += duration
    return tuple(result)


def _workload_report(
    index: int,
    *,
    samples: tuple[CapacitySample, ...] | None = None,
    claimed_pass: bool = True,
    claimed_p95_microseconds: int | None = None,
    claimed_wall_microseconds: int | None = None,
    claimed_peak_resources: ResourceObservation | None = None,
) -> CapacityWorkloadReport:
    definition = POLICY.workloads[index]
    selected = samples or _samples(count=definition.sample_count)
    durations = sorted(
        item.finished_at_microseconds - item.started_at_microseconds
        for item in selected
    )
    p95_index = (95 * len(durations) + 99) // 100 - 1
    wall = max(item.finished_at_microseconds for item in selected) - min(
        item.started_at_microseconds for item in selected
    )
    peak = ResourceObservation(
        cpu_millicores=max(item.resources.cpu_millicores for item in selected),
        ram_mebibytes=max(item.resources.ram_mebibytes for item in selected),
        pid_count=max(item.resources.pid_count for item in selected),
        process_count=max(item.resources.process_count for item in selected),
        in_flight_count=max(item.resources.in_flight_count for item in selected),
    )
    return CapacityWorkloadReport(
        workload=definition.workload,
        sample_count=definition.sample_count,
        concurrency=definition.concurrency,
        claimed_p95_microseconds=(
            durations[p95_index]
            if claimed_p95_microseconds is None
            else claimed_p95_microseconds
        ),
        claimed_wall_microseconds=(
            wall if claimed_wall_microseconds is None else claimed_wall_microseconds
        ),
        claimed_peak_resources=claimed_peak_resources or peak,
        claimed_pass=claimed_pass,
        samples=selected,
    )


def _report(
    *, workloads: tuple[CapacityWorkloadReport, ...] | None = None
) -> CapacityReport:
    return CapacityReport(
        schema_version=1,
        policy_id="stonks-capacity/1",
        report_id="capacity_20260722t010000z",
        execution_mode="paper",
        scope=POLICY.scope,
        resource_observation_scope="probe_process",
        workloads=workloads
        or tuple(_workload_report(index) for index in range(len(POLICY.workloads))),
    )


def test_verifier_recomputes_complete_report_and_returns_frozen_result() -> None:
    verification = verify_capacity_report(POLICY, _report())

    assert verification.passed is True
    assert verification.resource_observation_scope == "probe_process"
    assert verification.scope.evidence_class == "single_host_ci_baseline"
    assert tuple(item.workload for item in verification.workloads) == tuple(
        item.workload for item in POLICY.workloads
    )
    assert all(item.p95_microseconds == 1_000 for item in verification.workloads)
    assert all(item.wall_microseconds == 20_000 for item in verification.workloads)
    with pytest.raises(ValidationError):
        verification.passed = False  # type: ignore[misc]


def test_nearest_rank_p95_is_recomputed_from_raw_integer_samples() -> None:
    durations = tuple(range(1, 21))
    first = _workload_report(
        0,
        samples=_samples(count=20, durations=durations),
        claimed_p95_microseconds=19,
        claimed_wall_microseconds=sum(durations),
    )
    workloads = (
        first,
        *(_workload_report(index) for index in range(1, len(POLICY.workloads))),
    )

    verification = verify_capacity_report(POLICY, _report(workloads=workloads))

    assert verification.workloads[0].p95_microseconds == 19


def test_honest_limit_breach_is_verified_but_does_not_pass() -> None:
    definition = POLICY.workloads[0]
    durations = (definition.p95_ceiling_microseconds + 1,) * definition.sample_count
    samples = _samples(count=definition.sample_count, durations=durations)
    failed = _workload_report(
        0,
        samples=samples,
        claimed_pass=False,
        claimed_p95_microseconds=durations[0],
        claimed_wall_microseconds=sum(durations),
    )
    workloads = (
        failed,
        *(_workload_report(index) for index in range(1, len(POLICY.workloads))),
    )

    verification = verify_capacity_report(POLICY, _report(workloads=workloads))

    assert verification.passed is False
    assert verification.workloads[0].passed is False


@pytest.mark.parametrize(
    "change",
    (
        {"claimed_p95_microseconds": 999},
        {"claimed_wall_microseconds": 19_999},
        {"claimed_peak_resources": _resources(cpu_millicores=249)},
        {"claimed_pass": False},
    ),
)
def test_caller_cannot_forge_aggregates_or_pass(change: dict[str, object]) -> None:
    forged = _workload_report(0).model_copy(update=change)
    workloads = (
        forged,
        *(_workload_report(index) for index in range(1, len(POLICY.workloads))),
    )

    with pytest.raises(CapacityVerificationError) as raised:
        verify_capacity_report(POLICY, _report(workloads=workloads))

    assert raised.value.reason.value == "report_mismatch"


def test_caller_cannot_claim_pass_when_raw_samples_breach_resources() -> None:
    budget = POLICY.probe_runtime_budget
    overloaded = _resources(cpu_millicores=budget.cpu_millicores_ceiling + 1)
    samples = _samples(count=20, resources=overloaded)
    forged = _workload_report(0, samples=samples, claimed_pass=True)
    workloads = (
        forged,
        *(_workload_report(index) for index in range(1, len(POLICY.workloads))),
    )

    with pytest.raises(CapacityVerificationError, match="failed closed"):
        verify_capacity_report(POLICY, _report(workloads=workloads))


def test_wrong_scope_and_unsafe_copied_policy_fail_closed() -> None:
    wrong_scope = POLICY.scope.model_copy(update={"environment": "local"})
    report = _report().model_copy(update={"scope": wrong_scope})
    unsafe_measurement = POLICY.measurement.model_copy(
        update={"production_sla_claim": True}
    )
    unsafe_policy = POLICY.model_copy(update={"measurement": unsafe_measurement})

    with pytest.raises(CapacityVerificationError) as scope_error:
        verify_capacity_report(POLICY, report)
    with pytest.raises(CapacityVerificationError) as policy_error:
        verify_capacity_report(unsafe_policy, _report())

    assert scope_error.value.reason.value == "scope_mismatch"
    assert policy_error.value.reason.value == "unsafe_policy"


def test_resource_observation_scope_is_revalidated_for_policy_and_report() -> None:
    report = _report().model_copy(update={"resource_observation_scope": "host"})
    policy = POLICY.model_copy(update={"resource_observation_scope": "host"})

    with pytest.raises(CapacityVerificationError) as report_error:
        verify_capacity_report(POLICY, report)
    with pytest.raises(CapacityVerificationError) as policy_error:
        verify_capacity_report(policy, _report())

    assert report_error.value.reason.value == "invalid_report"
    assert policy_error.value.reason.value == "unsafe_policy"


def test_report_rejects_nonbaseline_capacity_evidence_class() -> None:
    wrong_scope = POLICY.scope.model_copy(update={"evidence_class": "adhoc"})
    report = _report().model_copy(update={"scope": wrong_scope})

    with pytest.raises(CapacityVerificationError) as raised:
        verify_capacity_report(POLICY, report)

    assert raised.value.reason.value == "invalid_report"


def test_workload_resources_use_probe_budget_not_deployment_budgets() -> None:
    core = POLICY.process_budgets[0]
    probe = POLICY.probe_runtime_budget
    assert core.cpu_millicores_ceiling < probe.cpu_millicores_ceiling
    observed = _resources(cpu_millicores=core.cpu_millicores_ceiling + 1)
    samples = _samples(count=20, resources=observed)
    honest_success = _workload_report(
        3,
        samples=samples,
        claimed_peak_resources=observed,
    )
    workloads = tuple(
        honest_success if index == 3 else _workload_report(index)
        for index in range(len(POLICY.workloads))
    )

    verification = verify_capacity_report(POLICY, _report(workloads=workloads))

    assert verification.workloads[3].passed is True


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("started_at_microseconds", -1),
        ("started_at_microseconds", nan),
        ("finished_at_microseconds", 86_400_000_001),
        ("finished_at_microseconds", 0),
        ("sample_id", True),
    ),
)
def test_raw_samples_reject_negative_nan_overflow_and_invalid_intervals(
    field: str, value: object
) -> None:
    values: dict[str, object] = {
        "sample_id": 1,
        "started_at_microseconds": 0,
        "finished_at_microseconds": 1,
        "resources": _resources(),
    }
    values[field] = value

    with pytest.raises(ValidationError):
        CapacitySample.model_validate(values)


@pytest.mark.parametrize(
    "field",
    ("ram_mebibytes", "pid_count", "process_count", "in_flight_count"),
)
def test_unknown_zero_resource_observations_fail_closed(field: str) -> None:
    with pytest.raises(ValidationError):
        _resources(**{field: 0})


def test_report_rejects_duplicate_missing_or_unknown_workloads_and_samples() -> None:
    reports = tuple(_workload_report(index) for index in range(len(POLICY.workloads)))
    duplicate_workloads = (*reports[:-1], reports[0])
    duplicate_samples = reports[0].model_copy(
        update={"samples": (*reports[0].samples[:-1], reports[0].samples[0])}
    )
    unknown = reports[0].model_copy(update={"workload": "unknown"})
    missing_sample_id = reports[0].model_copy(
        update={
            "samples": (
                *reports[0].samples[:-1],
                reports[0].samples[-1].model_copy(update={"sample_id": 21}),
            )
        }
    )

    for workloads in (
        reports[:-1],
        duplicate_workloads,
        (duplicate_samples, *reports[1:]),
        (missing_sample_id, *reports[1:]),
        (unknown, *reports[1:]),
    ):
        unsafe = _report().model_copy(update={"workloads": workloads})
        with pytest.raises(CapacityVerificationError):
            verify_capacity_report(POLICY, unsafe)
