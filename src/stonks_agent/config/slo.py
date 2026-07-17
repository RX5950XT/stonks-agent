"""Strict, versioned service-level objective policy configuration."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from stonks_agent.domain.errors import ErrorCode, StructuredError


class SLOCategory(StrEnum):
    CORRECTNESS = "correctness"
    AVAILABILITY = "availability"
    LATENCY = "latency"
    COST = "cost"


class IndicatorKind(StrEnum):
    VIOLATION_COUNT = "violation_count"
    SUCCESS_RATIO = "success_ratio"
    HISTOGRAM_QUANTILE = "histogram_quantile"


class Comparison(StrEnum):
    EQUAL = "equal"
    AT_LEAST = "at_least"
    AT_MOST = "at_most"


class TargetUnit(StrEnum):
    EVENTS = "events"
    RATIO = "ratio"
    SECONDS = "seconds"


class ErrorBudgetKind(StrEnum):
    EVENTS = "events"
    FRACTION = "fraction"


class BreachAction(StrEnum):
    DEGRADED = "degraded"
    FAILED = "failed"


class AlertSeverity(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"


class MissingDataPolicy(StrEnum):
    BREACH = "breach"


class RoutingState(StrEnum):
    POLICY_ONLY = "policy_only"


class BackendState(StrEnum):
    NONE = "none"


class MonitoringTopology(StrEnum):
    SINGLE_HOST = "single_host"


class StorageState(StrEnum):
    EPHEMERAL = "ephemeral"
    NONE = "none"


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MetricDefinition(StrictFrozenModel):
    metric_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    name: str = Field(pattern=r"^[a-z][a-z0-9_:]{0,127}$")
    labels: tuple[str, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_labels(self) -> Self:
        if len(self.labels) != len(set(self.labels)):
            raise ValueError("metric labels must be unique")
        if any(
            not 1 <= len(label) <= 32
            or label.strip() != label
            or not label.replace("_", "").isalnum()
            or not label[0].isalpha()
            for label in self.labels
        ):
            raise ValueError("metric labels must be bounded identifiers")
        return self


class MetricFilter(StrictFrozenModel):
    name: str = Field(min_length=1, max_length=32)
    value: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_canonical_value(self) -> Self:
        if (
            self.name.strip() != self.name
            or self.value.strip() != self.value
            or any(character.isspace() for character in self.name)
            or any(character.isspace() for character in self.value)
            or any(ord(character) < 32 for character in self.name + self.value)
        ):
            raise ValueError("metric filter must be canonical")
        return self


class SLOIndicator(StrictFrozenModel):
    metric_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    kind: IndicatorKind
    filters: tuple[MetricFilter, ...] = Field(min_length=1, max_length=4)
    good_label: str | None = Field(default=None, min_length=1, max_length=32)
    good_values: tuple[str, ...] = Field(default=(), max_length=4)
    quantile: str | None = Field(default=None, pattern=r"^0\.[0-9]{1,6}$")

    @model_validator(mode="after")
    def validate_kind_fields(self) -> Self:
        if len({item.name for item in self.filters}) != len(self.filters):
            raise ValueError("indicator filters must be unique")
        if self.kind is IndicatorKind.VIOLATION_COUNT:
            if self.good_label is not None or self.good_values or self.quantile:
                raise ValueError("violation indicator has unsupported fields")
        elif self.kind is IndicatorKind.SUCCESS_RATIO:
            if self.good_label is None or not self.good_values or self.quantile:
                raise ValueError("success-ratio indicator is incomplete")
            if len(self.good_values) != len(set(self.good_values)):
                raise ValueError("good values must be unique")
        elif (
            self.quantile is None
            or self.good_label is not None
            or self.good_values
            or not Decimal("0") < Decimal(self.quantile) < Decimal("1")
        ):
            raise ValueError("histogram-quantile indicator is incomplete")
        return self


class ObjectiveTarget(StrictFrozenModel):
    comparison: Comparison
    value: str = Field(min_length=1, max_length=32)
    unit: TargetUnit
    window_seconds: int = Field(ge=60, le=31_536_000)

    @model_validator(mode="after")
    def validate_value(self) -> Self:
        value = _canonical_decimal(self.value)
        if value < 0:
            raise ValueError("target cannot be negative")
        if self.unit is TargetUnit.RATIO and value > 1:
            raise ValueError("ratio target must be between zero and one")
        return self


class ErrorBudget(StrictFrozenModel):
    kind: ErrorBudgetKind
    value: str = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_value(self) -> Self:
        value = _canonical_decimal(self.value)
        if value < 0:
            raise ValueError("error budget cannot be negative")
        if self.kind is ErrorBudgetKind.FRACTION and value > 1:
            raise ValueError("fraction error budget must be between zero and one")
        return self


class BreachResponse(StrictFrozenModel):
    severity: AlertSeverity
    action: BreachAction
    hold_seconds: int = Field(ge=0, le=86_400)
    route_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    runbook_anchor: str = Field(pattern=r"^[a-z][a-z0-9-]{0,127}$")


class SLOObjective(StrictFrozenModel):
    objective_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    category: SLOCategory
    indicator: SLOIndicator
    target: ObjectiveTarget
    error_budget: ErrorBudget
    missing_data: MissingDataPolicy
    breach: BreachResponse


class BurnPolicy(StrictFrozenModel):
    policy_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    objective_ids: tuple[str, ...] = Field(min_length=4, max_length=4)
    window_seconds: int = Field(ge=60, le=31_536_000)
    burn_rate: str = Field(min_length=1, max_length=32)
    hold_seconds: int = Field(ge=0, le=86_400)
    severity: AlertSeverity
    action: BreachAction
    route_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")

    @model_validator(mode="after")
    def validate_burn_rate(self) -> Self:
        if not Decimal("0") < _canonical_decimal(self.burn_rate) <= Decimal("1000"):
            raise ValueError("burn rate must be positive and bounded")
        return self


class GuardedStatusBehavior(StrictFrozenModel):
    allow_new_target: Literal[False]
    allow_new_reservation: Literal[False]
    allow_new_order: Literal[False]
    stop_mode: Literal["after_current_boundary", "immediate"]


class BudgetBreachBehavior(StrictFrozenModel):
    execution_mode: Literal["paper"]
    preserve_observed_commit: Literal[True]
    degraded: GuardedStatusBehavior
    failed: GuardedStatusBehavior
    allow_order_chasing: Literal[False]
    allow_compensating_quantity: Literal[False]


class OperatorRoute(StrictFrozenModel):
    route_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    severity: AlertSeverity
    receiver: Literal["paper_operator"]
    delivery: Literal["page_required"]
    configured: Literal[False]
    runbook: Literal["docs/operations/slo.md"]


class OperatorRouting(StrictFrozenModel):
    state: RoutingState
    paging_backend: BackendState
    delivery_guarantee: BackendState
    routes: tuple[OperatorRoute, ...] = Field(min_length=2, max_length=2)


class MonitoringLimitations(StrictFrozenModel):
    topology: MonitoringTopology
    prometheus_storage: StorageState
    paging_backend: BackendState
    trace_storage: StorageState


class SLOPolicy(StrictFrozenModel):
    schema_version: Literal[1]
    policy_id: Literal["stonks-slo/1"]
    execution_mode: Literal["paper"]
    metrics: tuple[MetricDefinition, ...] = Field(min_length=6, max_length=6)
    objectives: tuple[SLOObjective, ...] = Field(min_length=12, max_length=12)
    burn_policies: tuple[BurnPolicy, ...] = Field(min_length=3, max_length=3)
    budget_breach_behavior: BudgetBreachBehavior
    operator_routing: OperatorRouting
    monitoring_limitations: MonitoringLimitations

    @model_validator(mode="after")
    def validate_complete_policy(self) -> Self:
        _validate_metric_catalog(self.metrics)
        _validate_objectives(self.metrics, self.objectives)
        _validate_burn_policies(self.burn_policies)
        _validate_routes(self.operator_routing, self.objectives, self.burn_policies)
        return self


class SLOPolicyLoadError(RuntimeError):
    def __init__(self, error: StructuredError) -> None:
        self.error = error
        super().__init__("SLO policy configuration is invalid")


def load_slo_policy(path: Path) -> SLOPolicy:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return SLOPolicy.model_validate(payload)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as error:
        raise SLOPolicyLoadError(
            StructuredError(
                code=ErrorCode.CONFIGURATION_INVALID,
                message="SLO policy configuration is invalid",
                details={"file": path.name},
            )
        ) from error


_METRIC_CATALOG = (
    (
        "correctness_violations",
        "stonks_correctness_violations_total",
        ("invariant", "environment"),
    ),
    (
        "api_requests",
        "stonks_api_requests_total",
        ("component", "operation", "status", "environment"),
    ),
    (
        "operation_calls",
        "stonks_operation_calls_total",
        ("component", "operation", "status", "environment"),
    ),
    (
        "operation_duration",
        "stonks_operation_duration_seconds",
        ("component", "operation", "status", "environment"),
    ),
    (
        "budget_usage",
        "stonks_budget_usage_ratio",
        ("budget", "scope", "environment"),
    ),
    (
        "budget_outcomes",
        "stonks_budget_outcomes_total",
        ("budget", "scope", "outcome", "environment"),
    ),
)

_OBJECTIVE_CATALOG = (
    (
        "duplicate_paper_order",
        SLOCategory.CORRECTNESS,
        "correctness_violations",
        IndicatorKind.VIOLATION_COUNT,
        (("invariant", "duplicate_paper_order"),),
    ),
    (
        "future_evidence",
        SLOCategory.CORRECTNESS,
        "correctness_violations",
        IndicatorKind.VIOLATION_COUNT,
        (("invariant", "future_evidence"),),
    ),
    (
        "claim_provenance",
        SLOCategory.CORRECTNESS,
        "correctness_violations",
        IndicatorKind.VIOLATION_COUNT,
        (("invariant", "claim_provenance"),),
    ),
    (
        "risk_replayability",
        SLOCategory.CORRECTNESS,
        "correctness_violations",
        IndicatorKind.VIOLATION_COUNT,
        (("invariant", "risk_replayability"),),
    ),
    (
        "api_availability",
        SLOCategory.AVAILABILITY,
        "api_requests",
        IndicatorKind.SUCCESS_RATIO,
        (("component", "api"), ("operation", "http_request")),
    ),
    (
        "paper_cycle_availability",
        SLOCategory.AVAILABILITY,
        "operation_calls",
        IndicatorKind.SUCCESS_RATIO,
        (("component", "worker"), ("operation", "process")),
    ),
    (
        "api_request_latency",
        SLOCategory.LATENCY,
        "operation_duration",
        IndicatorKind.HISTOGRAM_QUANTILE,
        (("component", "api"), ("operation", "http_request")),
    ),
    (
        "worker_process_latency",
        SLOCategory.LATENCY,
        "operation_duration",
        IndicatorKind.HISTOGRAM_QUANTILE,
        (("component", "worker"), ("operation", "process")),
    ),
    (
        "research_latency_budget",
        SLOCategory.LATENCY,
        "budget_usage",
        IndicatorKind.HISTOGRAM_QUANTILE,
        (("budget", "latency"), ("scope", "research")),
    ),
    (
        "paper_cycle_latency_budget",
        SLOCategory.LATENCY,
        "budget_usage",
        IndicatorKind.HISTOGRAM_QUANTILE,
        (("budget", "latency"), ("scope", "paper_cycle")),
    ),
    (
        "research_cost_budget",
        SLOCategory.COST,
        "budget_usage",
        IndicatorKind.HISTOGRAM_QUANTILE,
        (("budget", "cost"), ("scope", "research")),
    ),
    (
        "paper_cycle_cost_budget",
        SLOCategory.COST,
        "budget_usage",
        IndicatorKind.HISTOGRAM_QUANTILE,
        (("budget", "cost"), ("scope", "paper_cycle")),
    ),
)


def _canonical_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("value must be a finite canonical decimal") from error
    if not parsed.is_finite() or format(parsed, "f") != value:
        raise ValueError("value must be a finite canonical decimal")
    return parsed


def _validate_metric_catalog(metrics: tuple[MetricDefinition, ...]) -> None:
    if len({metric.metric_id for metric in metrics}) != len(metrics):
        raise ValueError("metric ids must be unique")
    if len({metric.name for metric in metrics}) != len(metrics):
        raise ValueError("metric names must be unique")
    actual = tuple((item.metric_id, item.name, item.labels) for item in metrics)
    if actual != _METRIC_CATALOG:
        raise ValueError("metric catalog drifted")


def _validate_objectives(
    metrics: tuple[MetricDefinition, ...],
    objectives: tuple[SLOObjective, ...],
) -> None:
    if len({objective.objective_id for objective in objectives}) != len(objectives):
        raise ValueError("objective ids must be unique")
    catalog = {metric.metric_id: metric for metric in metrics}
    for objective in objectives:
        metric = catalog.get(objective.indicator.metric_id)
        if metric is None:
            raise ValueError("indicator metric is not declared")
        _validate_objective_indicator(catalog, objective)
    actual = tuple(
        (
            item.objective_id,
            item.category,
            item.indicator.metric_id,
            item.indicator.kind,
            tuple((label.name, label.value) for label in item.indicator.filters),
        )
        for item in objectives
    )
    if actual != _OBJECTIVE_CATALOG:
        raise ValueError("objective catalog is incomplete or reordered")
    for objective in objectives:
        _validate_objective_threshold(objective)


def _validate_objective_indicator(
    catalog: dict[str, MetricDefinition],
    objective: SLOObjective,
) -> None:
    metric = catalog[objective.indicator.metric_id]
    allowed = set(metric.labels) - {"environment"}
    used = {item.name for item in objective.indicator.filters}
    if not used <= allowed or (
        objective.indicator.good_label is not None
        and objective.indicator.good_label not in allowed
    ):
        raise ValueError("indicator filters must match the metric label catalog")
    if objective.indicator.kind is IndicatorKind.SUCCESS_RATIO and (
        objective.indicator.good_label != "status"
        or objective.indicator.good_values != ("success",)
    ):
        raise ValueError("availability success vocabulary drifted")
    if objective.indicator.kind is IndicatorKind.HISTOGRAM_QUANTILE and (
        objective.indicator.quantile != "0.95"
    ):
        raise ValueError("latency and cost quantile must remain p95")


def _validate_objective_threshold(objective: SLOObjective) -> None:
    target = objective.target
    budget = objective.error_budget
    if target.window_seconds != 2_592_000:
        raise ValueError("SLO objective window must remain 30 days")
    if objective.category is SLOCategory.CORRECTNESS:
        if (
            target.comparison is not Comparison.EQUAL
            or target.value != "0"
            or target.unit is not TargetUnit.EVENTS
            or budget.kind is not ErrorBudgetKind.EVENTS
            or budget.value != "0"
            or objective.breach.severity is not AlertSeverity.CRITICAL
            or objective.breach.action is not BreachAction.FAILED
            or objective.breach.hold_seconds != 0
        ):
            raise ValueError("correctness objectives require a zero error budget")
        return
    expected = {
        SLOCategory.AVAILABILITY: (
            Comparison.AT_LEAST,
            "0.99",
            TargetUnit.RATIO,
            "0.01",
        ),
        SLOCategory.LATENCY: (
            Comparison.AT_MOST,
            (
                "2"
                if objective.objective_id == "api_request_latency"
                else "30"
                if objective.objective_id == "worker_process_latency"
                else "1"
            ),
            (
                TargetUnit.SECONDS
                if objective.objective_id
                in {"api_request_latency", "worker_process_latency"}
                else TargetUnit.RATIO
            ),
            "0.05",
        ),
        SLOCategory.COST: (
            Comparison.AT_MOST,
            "1",
            TargetUnit.RATIO,
            "0.05",
        ),
    }[objective.category]
    if (
        target.comparison,
        target.value,
        target.unit,
        budget.value,
    ) != expected or budget.kind is not ErrorBudgetKind.FRACTION:
        raise ValueError("non-correctness objective threshold drifted")


def _validate_burn_policies(policies: tuple[BurnPolicy, ...]) -> None:
    objective_ids = (
        "research_latency_budget",
        "paper_cycle_latency_budget",
        "research_cost_budget",
        "paper_cycle_cost_budget",
    )
    actual = tuple(
        (
            item.policy_id,
            item.objective_ids,
            item.window_seconds,
            item.burn_rate,
            item.hold_seconds,
            item.severity,
            item.action,
        )
        for item in policies
    )
    expected = (
        (
            "fast",
            objective_ids,
            300,
            "14.4",
            120,
            AlertSeverity.CRITICAL,
            BreachAction.FAILED,
        ),
        (
            "slow",
            objective_ids,
            3_600,
            "6",
            900,
            AlertSeverity.WARNING,
            BreachAction.DEGRADED,
        ),
        (
            "exhausted",
            objective_ids,
            2_592_000,
            "1",
            0,
            AlertSeverity.CRITICAL,
            BreachAction.FAILED,
        ),
    )
    if actual != expected:
        raise ValueError("error-budget burn policy drifted")


def _validate_routes(
    routing: OperatorRouting,
    objectives: tuple[SLOObjective, ...],
    burn_policies: tuple[BurnPolicy, ...],
) -> None:
    expected = (
        ("critical_paper_operator", AlertSeverity.CRITICAL),
        ("warning_paper_operator", AlertSeverity.WARNING),
    )
    actual = tuple((route.route_id, route.severity) for route in routing.routes)
    if actual != expected:
        raise ValueError("operator route catalog drifted")
    routes = {route.route_id: route for route in routing.routes}
    responses: tuple[BreachResponse | BurnPolicy, ...] = (
        tuple(objective.breach for objective in objectives) + burn_policies
    )
    for response in responses:
        route = routes.get(response.route_id)
        if route is None or route.severity is not response.severity:
            raise ValueError("breach response does not match operator route")
