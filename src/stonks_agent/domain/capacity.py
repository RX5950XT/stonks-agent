"""Frozen, fail-closed capacity contracts for the paper deployment."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from stonks_agent.domain.telemetry import MetricKind, validate_metric_measurement

MAX_MICROSECONDS = 86_400_000_000
MAX_SAMPLE_COUNT = 10_000
MAX_CONCURRENCY = 1_024


class CapacityWorkload(StrEnum):
    API = "api"
    QUEUE = "queue"
    SNAPSHOT = "snapshot"
    RESEARCH = "research"
    FORECAST = "forecast"
    PAPER_CYCLE = "paper_cycle"


class ProcessBudgetId(StrEnum):
    CORE = "core"
    POSTGRES = "postgres"
    TRADINGAGENTS = "tradingagents"
    KRONOS_CPU = "kronos_cpu"
    KRONOS_CUDA = "kronos_cuda"
    QUANT_LAB = "quant_lab"


class CapacityVerificationReason(StrEnum):
    UNSAFE_POLICY = "unsafe_policy"
    INVALID_REPORT = "invalid_report"
    CONTRACT_MISMATCH = "contract_mismatch"
    SCOPE_MISMATCH = "scope_mismatch"
    REPORT_MISMATCH = "report_mismatch"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CapacityScope(_FrozenModel):
    environment: Literal["local", "development", "test"]
    topology: Literal["single_host"]
    dataset: Literal["synthetic_bounded"]
    hardware_profile: Literal["ci_reference_v1"]
    evidence_class: Literal["single_host_ci_baseline"]


class CapacityMeasurementContract(_FrozenModel):
    duration_unit: Literal["microseconds"]
    percentile: Literal[95]
    percentile_method: Literal["nearest_rank"]
    minimum_sample_count: int = Field(strict=True, ge=2, le=MAX_SAMPLE_COUNT)
    maximum_sample_count: int = Field(strict=True, ge=2, le=MAX_SAMPLE_COUNT)
    maximum_concurrency: int = Field(strict=True, ge=1, le=MAX_CONCURRENCY)
    wall_clock_ceiling_microseconds: int = Field(strict=True, ge=1, le=MAX_MICROSECONDS)
    production_sla_claim: Literal[False]

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.minimum_sample_count > self.maximum_sample_count:
            raise ValueError("sample count bounds are inverted")
        return self


class ProcessBudget(_FrozenModel):
    process_id: ProcessBudgetId
    cpu_millicores_ceiling: int = Field(strict=True, ge=1, le=64_000)
    ram_mebibytes_ceiling: int = Field(strict=True, ge=1, le=1_048_576)
    pid_ceiling: int = Field(strict=True, ge=1, le=65_535)
    process_ceiling: int = Field(strict=True, ge=1, le=65_535)
    in_flight_ceiling: int = Field(strict=True, ge=1, le=MAX_CONCURRENCY)
    gpu_vram_enforced: Literal[False] | None = None

    @model_validator(mode="after")
    def validate_process_budget(self) -> Self:
        if self.process_ceiling > self.pid_ceiling:
            raise ValueError("process ceiling cannot exceed PID ceiling")
        if (
            self.process_id is ProcessBudgetId.KRONOS_CUDA
            and self.gpu_vram_enforced is not False
        ):
            raise ValueError("CUDA budget must disclose unenforced GPU VRAM")
        if (
            self.process_id is not ProcessBudgetId.KRONOS_CUDA
            and self.gpu_vram_enforced is not None
        ):
            raise ValueError("GPU VRAM disclosure is only valid for CUDA")
        return self


class ProbeRuntimeBudget(_FrozenModel):
    cpu_millicores_ceiling: int = Field(strict=True, ge=1, le=64_000)
    ram_mebibytes_ceiling: int = Field(strict=True, ge=1, le=1_048_576)
    pid_ceiling: int = Field(strict=True, ge=1, le=65_535)
    process_ceiling: int = Field(strict=True, ge=1, le=65_535)
    in_flight_ceiling: int = Field(strict=True, ge=1, le=MAX_CONCURRENCY)

    @model_validator(mode="after")
    def validate_process_budget(self) -> Self:
        if self.process_ceiling > self.pid_ceiling:
            raise ValueError("probe process ceiling cannot exceed PID ceiling")
        return self


class CapacityMetricLabel(_FrozenModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    value: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")


class CapacityMetricBinding(_FrozenModel):
    name: Literal["stonks_operation_duration_seconds"]
    labels: tuple[CapacityMetricLabel, ...] = Field(min_length=4, max_length=8)

    @model_validator(mode="after")
    def validate_telemetry_catalog(self) -> Self:
        if len({item.name for item in self.labels}) != len(self.labels):
            raise ValueError("metric labels must not be duplicated")
        validate_metric_measurement(
            self.name,
            MetricKind.HISTOGRAM,
            0,
            {item.name: item.value for item in self.labels},
        )
        return self


class CapacityWorkloadDefinition(_FrozenModel):
    workload: CapacityWorkload
    sample_count: int = Field(strict=True, ge=1, le=MAX_SAMPLE_COUNT)
    concurrency: int = Field(strict=True, ge=1, le=MAX_CONCURRENCY)
    p95_ceiling_microseconds: int = Field(strict=True, ge=1, le=MAX_MICROSECONDS)
    wall_ceiling_microseconds: int = Field(strict=True, ge=1, le=MAX_MICROSECONDS)
    metric: CapacityMetricBinding

    @model_validator(mode="after")
    def validate_latency_bounds(self) -> Self:
        if self.p95_ceiling_microseconds > self.wall_ceiling_microseconds:
            raise ValueError("p95 ceiling cannot exceed wall ceiling")
        return self


class CapacityPolicy(_FrozenModel):
    schema_version: Literal[1]
    policy_id: Literal["stonks-capacity/1"]
    execution_mode: Literal["paper"]
    scope: CapacityScope
    resource_observation_scope: Literal["probe_process"]
    process_budget_contract: Literal["static_manifest_only"]
    probe_runtime_budget: ProbeRuntimeBudget
    measurement: CapacityMeasurementContract
    process_budgets: tuple[ProcessBudget, ...] = Field(min_length=6, max_length=6)
    workloads: tuple[CapacityWorkloadDefinition, ...] = Field(
        min_length=6, max_length=6
    )

    @model_validator(mode="after")
    def validate_closed_policy(self) -> Self:
        actual = tuple(item.workload for item in self.workloads)
        if actual != WORKLOAD_CATALOG:
            raise ValueError("workload catalog is incomplete, duplicated, or reordered")
        if tuple(item.process_id for item in self.process_budgets) != (
            PROCESS_BUDGET_CATALOG
        ):
            raise ValueError("process budget catalog is incomplete or reordered")
        _validate_workload_bounds(self)
        _validate_metric_bindings(self)
        _validate_policy_contract(self)
        return self

    def definition_for(self, workload: CapacityWorkload) -> CapacityWorkloadDefinition:
        return self.workloads[WORKLOAD_CATALOG.index(workload)]


class ResourceObservation(_FrozenModel):
    cpu_millicores: int = Field(strict=True, ge=0, le=64_000)
    ram_mebibytes: int = Field(strict=True, ge=1, le=1_048_576)
    pid_count: int = Field(strict=True, ge=1, le=65_535)
    process_count: int = Field(strict=True, ge=1, le=65_535)
    in_flight_count: int = Field(strict=True, ge=1, le=MAX_CONCURRENCY)

    @model_validator(mode="after")
    def validate_process_count(self) -> Self:
        if self.process_count > self.pid_count:
            raise ValueError("process count cannot exceed PID count")
        return self


class CapacitySample(_FrozenModel):
    sample_id: int = Field(strict=True, ge=1, le=MAX_SAMPLE_COUNT)
    started_at_microseconds: int = Field(strict=True, ge=0, le=MAX_MICROSECONDS)
    finished_at_microseconds: int = Field(strict=True, ge=1, le=MAX_MICROSECONDS)
    resources: ResourceObservation

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if self.finished_at_microseconds <= self.started_at_microseconds:
            raise ValueError("sample interval must be positive")
        return self


class CapacityWorkloadReport(_FrozenModel):
    workload: CapacityWorkload
    sample_count: int = Field(strict=True, ge=1, le=MAX_SAMPLE_COUNT)
    concurrency: int = Field(strict=True, ge=1, le=MAX_CONCURRENCY)
    claimed_p95_microseconds: int = Field(strict=True, ge=1, le=MAX_MICROSECONDS)
    claimed_wall_microseconds: int = Field(strict=True, ge=1, le=MAX_MICROSECONDS)
    claimed_peak_resources: ResourceObservation
    claimed_pass: bool = Field(strict=True)
    samples: tuple[CapacitySample, ...] = Field(
        min_length=1, max_length=MAX_SAMPLE_COUNT
    )

    @model_validator(mode="after")
    def validate_sample_ids(self) -> Self:
        identifiers = tuple(item.sample_id for item in self.samples)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("sample identifiers must not be duplicated")
        if identifiers != tuple(range(1, len(identifiers) + 1)):
            raise ValueError("sample identifiers must be complete and ordered")
        return self


class CapacityReport(_FrozenModel):
    schema_version: Literal[1]
    policy_id: Literal["stonks-capacity/1"]
    report_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    execution_mode: Literal["paper"]
    scope: CapacityScope
    resource_observation_scope: Literal["probe_process"]
    workloads: tuple[CapacityWorkloadReport, ...] = Field(min_length=6, max_length=6)

    @model_validator(mode="after")
    def validate_workload_catalog(self) -> Self:
        actual = tuple(item.workload for item in self.workloads)
        if actual != WORKLOAD_CATALOG:
            raise ValueError(
                "report workload catalog is incomplete, duplicated, or reordered"
            )
        return self


class VerifiedWorkloadCapacity(_FrozenModel):
    workload: CapacityWorkload
    sample_count: int = Field(strict=True, ge=1, le=MAX_SAMPLE_COUNT)
    peak_concurrency: int = Field(strict=True, ge=1, le=MAX_CONCURRENCY)
    p95_microseconds: int = Field(strict=True, ge=1, le=MAX_MICROSECONDS)
    wall_microseconds: int = Field(strict=True, ge=1, le=MAX_MICROSECONDS)
    peak_resources: ResourceObservation
    passed: bool = Field(strict=True)


class CapacityVerification(_FrozenModel):
    schema_version: Literal[1]
    policy_id: Literal["stonks-capacity/1"]
    report_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    scope: CapacityScope
    resource_observation_scope: Literal["probe_process"]
    workloads: tuple[VerifiedWorkloadCapacity, ...] = Field(min_length=6, max_length=6)
    passed: bool = Field(strict=True)


class CapacityVerificationError(RuntimeError):
    def __init__(self, reason: CapacityVerificationReason) -> None:
        self.reason = reason
        super().__init__("Capacity report verification failed closed")


WORKLOAD_CATALOG = (
    CapacityWorkload.API,
    CapacityWorkload.QUEUE,
    CapacityWorkload.SNAPSHOT,
    CapacityWorkload.RESEARCH,
    CapacityWorkload.FORECAST,
    CapacityWorkload.PAPER_CYCLE,
)

PROCESS_BUDGET_CATALOG = (
    ProcessBudgetId.CORE,
    ProcessBudgetId.POSTGRES,
    ProcessBudgetId.TRADINGAGENTS,
    ProcessBudgetId.KRONOS_CPU,
    ProcessBudgetId.KRONOS_CUDA,
    ProcessBudgetId.QUANT_LAB,
)

_METRIC_BINDINGS = (
    (CapacityWorkload.API, "api", "http_request"),
    (CapacityWorkload.QUEUE, "queue", "claim"),
    (CapacityWorkload.SNAPSHOT, "provider", "fetch"),
    (CapacityWorkload.RESEARCH, "llm", "generate"),
    (CapacityWorkload.FORECAST, "model", "infer"),
    (CapacityWorkload.PAPER_CYCLE, "worker", "process"),
)

_MEASUREMENT_CONTRACT = (20, 1_000, 16, 600_000_000)

_PROBE_RUNTIME_BUDGET = (4_000, 2_048, 1, 1, 16)

_PROCESS_BUDGET_LIMITS = (
    (ProcessBudgetId.CORE, 500, 256, 64, 1, 128, None),
    (ProcessBudgetId.POSTGRES, 1_000, 512, 128, 64, 16, None),
    (ProcessBudgetId.TRADINGAGENTS, 2_000, 4_096, 256, 1, 1, None),
    (ProcessBudgetId.KRONOS_CPU, 4_000, 8_192, 256, 1, 1, None),
    (ProcessBudgetId.KRONOS_CUDA, 4_000, 12_288, 256, 1, 1, False),
    (ProcessBudgetId.QUANT_LAB, 2_000, 2_048, 128, 1, 1, None),
)

_WORKLOAD_LIMITS = (
    (CapacityWorkload.API, 20, 4, 2_000_000, 60_000_000),
    (CapacityWorkload.QUEUE, 20, 4, 5_000_000, 120_000_000),
    (CapacityWorkload.SNAPSHOT, 20, 2, 10_000_000, 240_000_000),
    (CapacityWorkload.RESEARCH, 20, 1, 30_000_000, 600_000_000),
    (CapacityWorkload.FORECAST, 20, 1, 30_000_000, 600_000_000),
    (CapacityWorkload.PAPER_CYCLE, 20, 1, 30_000_000, 600_000_000),
)


def verify_capacity_report(
    policy: CapacityPolicy, report: CapacityReport
) -> CapacityVerification:
    selected_policy = _revalidate_policy(policy)
    selected_report = _revalidate_report(report)
    if selected_report.scope != selected_policy.scope:
        raise CapacityVerificationError(CapacityVerificationReason.SCOPE_MISMATCH)
    if (
        selected_report.resource_observation_scope
        != selected_policy.resource_observation_scope
    ):
        raise CapacityVerificationError(CapacityVerificationReason.SCOPE_MISMATCH)
    verified = tuple(
        _verify_workload(selected_policy, definition, observed)
        for definition, observed in zip(
            selected_policy.workloads, selected_report.workloads, strict=True
        )
    )
    return CapacityVerification(
        schema_version=1,
        policy_id=selected_policy.policy_id,
        report_id=selected_report.report_id,
        scope=selected_policy.scope,
        resource_observation_scope=selected_policy.resource_observation_scope,
        workloads=verified,
        passed=all(item.passed for item in verified),
    )


def _revalidate_policy(policy: CapacityPolicy) -> CapacityPolicy:
    try:
        return CapacityPolicy.model_validate(policy.model_dump(warnings=False))
    except ValidationError as error:
        raise CapacityVerificationError(
            CapacityVerificationReason.UNSAFE_POLICY
        ) from error


def _revalidate_report(report: CapacityReport) -> CapacityReport:
    try:
        return CapacityReport.model_validate(report.model_dump(warnings=False))
    except ValidationError as error:
        raise CapacityVerificationError(
            CapacityVerificationReason.INVALID_REPORT
        ) from error


def _verify_workload(
    policy: CapacityPolicy,
    definition: CapacityWorkloadDefinition,
    observed: CapacityWorkloadReport,
) -> VerifiedWorkloadCapacity:
    if (
        observed.workload is not definition.workload
        or observed.sample_count != definition.sample_count
        or observed.concurrency != definition.concurrency
        or len(observed.samples) != definition.sample_count
    ):
        raise CapacityVerificationError(CapacityVerificationReason.CONTRACT_MISMATCH)
    verification = _calculate_workload(policy, definition, observed.samples)
    claimed = (
        observed.claimed_p95_microseconds,
        observed.claimed_wall_microseconds,
        observed.claimed_peak_resources,
        observed.claimed_pass,
    )
    computed = (
        verification.p95_microseconds,
        verification.wall_microseconds,
        verification.peak_resources,
        verification.passed,
    )
    if claimed != computed:
        raise CapacityVerificationError(CapacityVerificationReason.REPORT_MISMATCH)
    return verification


def _calculate_workload(
    policy: CapacityPolicy,
    definition: CapacityWorkloadDefinition,
    samples: tuple[CapacitySample, ...],
) -> VerifiedWorkloadCapacity:
    durations = sorted(
        item.finished_at_microseconds - item.started_at_microseconds for item in samples
    )
    p95 = durations[_nearest_rank_index(len(durations), 95)]
    wall = max(item.finished_at_microseconds for item in samples) - min(
        item.started_at_microseconds for item in samples
    )
    peak_concurrency = _peak_concurrency(samples)
    resources = _peak_resources(samples, peak_concurrency)
    passed = _within_limits(policy, definition, p95, wall, peak_concurrency, resources)
    return VerifiedWorkloadCapacity(
        workload=definition.workload,
        sample_count=len(samples),
        peak_concurrency=peak_concurrency,
        p95_microseconds=p95,
        wall_microseconds=wall,
        peak_resources=resources,
        passed=passed,
    )


def _nearest_rank_index(sample_count: int, percentile: int) -> int:
    return (percentile * sample_count + 99) // 100 - 1


def _peak_concurrency(samples: tuple[CapacitySample, ...]) -> int:
    events = sorted(
        (
            *((item.started_at_microseconds, 1) for item in samples),
            *((item.finished_at_microseconds, -1) for item in samples),
        )
    )
    current = 0
    peak = 0
    for _, change in events:
        current += change
        peak = max(peak, current)
    return peak


def _peak_resources(
    samples: tuple[CapacitySample, ...], peak_concurrency: int
) -> ResourceObservation:
    return ResourceObservation(
        cpu_millicores=max(item.resources.cpu_millicores for item in samples),
        ram_mebibytes=max(item.resources.ram_mebibytes for item in samples),
        pid_count=max(item.resources.pid_count for item in samples),
        process_count=max(item.resources.process_count for item in samples),
        in_flight_count=max(
            peak_concurrency,
            max(item.resources.in_flight_count for item in samples),
        ),
    )


def _within_limits(
    policy: CapacityPolicy,
    definition: CapacityWorkloadDefinition,
    p95: int,
    wall: int,
    peak_concurrency: int,
    resources: ResourceObservation,
) -> bool:
    budget = policy.probe_runtime_budget
    return all(
        (
            p95 <= definition.p95_ceiling_microseconds,
            wall <= definition.wall_ceiling_microseconds,
            wall <= policy.measurement.wall_clock_ceiling_microseconds,
            peak_concurrency <= definition.concurrency,
            resources.cpu_millicores <= budget.cpu_millicores_ceiling,
            resources.ram_mebibytes <= budget.ram_mebibytes_ceiling,
            resources.pid_count <= budget.pid_ceiling,
            resources.process_count <= budget.process_ceiling,
            resources.in_flight_count <= budget.in_flight_ceiling,
        )
    )


def _validate_workload_bounds(policy: CapacityPolicy) -> None:
    measurement = policy.measurement
    for workload in policy.workloads:
        if not (
            measurement.minimum_sample_count
            <= workload.sample_count
            <= measurement.maximum_sample_count
        ):
            raise ValueError("sample_count is outside measurement bounds")
        if not 1 <= workload.concurrency <= measurement.maximum_concurrency:
            raise ValueError("concurrency is outside measurement bounds")
        runtime_budget = policy.probe_runtime_budget
        if workload.concurrency > runtime_budget.in_flight_ceiling:
            raise ValueError("concurrency exceeds the in-flight resource budget")
        if (
            workload.wall_ceiling_microseconds
            > measurement.wall_clock_ceiling_microseconds
        ):
            raise ValueError("workload wall ceiling exceeds the bounded run")


def _validate_metric_bindings(policy: CapacityPolicy) -> None:
    actual = tuple(
        (
            item.workload,
            _metric_label(item.metric, "component"),
            _metric_label(item.metric, "operation"),
        )
        for item in policy.workloads
    )
    if actual != _METRIC_BINDINGS:
        raise ValueError("capacity metric bindings drifted")
    for item in policy.workloads:
        if (
            tuple(label.name for label in item.metric.labels)
            != ("component", "operation", "status", "environment")
            or _metric_label(item.metric, "status") != "success"
            or _metric_label(item.metric, "environment") != policy.scope.environment
        ):
            raise ValueError("capacity metric labels drifted")


def _metric_label(binding: CapacityMetricBinding, name: str) -> str:
    return next(item.value for item in binding.labels if item.name == name)


def _validate_policy_contract(policy: CapacityPolicy) -> None:
    scope = policy.scope
    if (
        scope.environment,
        scope.topology,
        scope.dataset,
        scope.hardware_profile,
        scope.evidence_class,
    ) != (
        "test",
        "single_host",
        "synthetic_bounded",
        "ci_reference_v1",
        "single_host_ci_baseline",
    ):
        raise ValueError("capacity scope drifted")
    measurement = policy.measurement
    if (
        measurement.minimum_sample_count,
        measurement.maximum_sample_count,
        measurement.maximum_concurrency,
        measurement.wall_clock_ceiling_microseconds,
    ) != _MEASUREMENT_CONTRACT:
        raise ValueError("capacity measurement contract drifted")
    probe_budget = policy.probe_runtime_budget
    if (
        probe_budget.cpu_millicores_ceiling,
        probe_budget.ram_mebibytes_ceiling,
        probe_budget.pid_ceiling,
        probe_budget.process_ceiling,
        probe_budget.in_flight_ceiling,
    ) != _PROBE_RUNTIME_BUDGET:
        raise ValueError("probe runtime budget drifted")
    _validate_exact_process_limits(policy.process_budgets)
    _validate_exact_workload_limits(policy.workloads)


def _validate_exact_process_limits(budgets: tuple[ProcessBudget, ...]) -> None:
    actual = tuple(
        (
            item.process_id,
            item.cpu_millicores_ceiling,
            item.ram_mebibytes_ceiling,
            item.pid_ceiling,
            item.process_ceiling,
            item.in_flight_ceiling,
            item.gpu_vram_enforced,
        )
        for item in budgets
    )
    if actual != _PROCESS_BUDGET_LIMITS:
        raise ValueError("process budget limits drifted")


def _validate_exact_workload_limits(
    workloads: tuple[CapacityWorkloadDefinition, ...],
) -> None:
    actual = tuple(
        (
            item.workload,
            item.sample_count,
            item.concurrency,
            item.p95_ceiling_microseconds,
            item.wall_ceiling_microseconds,
        )
        for item in workloads
    )
    if actual != _WORKLOAD_LIMITS:
        raise ValueError("capacity workload limits drifted")
