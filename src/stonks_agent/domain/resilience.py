"""Fail-closed contracts for paper-only resilience drills."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from stonks_agent.domain.telemetry import MetricKind, validate_metric_measurement


class FailureClass(StrEnum):
    UPSTREAM_OUTAGE = "upstream_outage"
    DATABASE_DISASTER = "database_disaster"
    WORKER_FAILURE = "worker_failure"
    DELIVERY_ANOMALY = "delivery_anomaly"
    ARTIFACT_CORRUPTION = "artifact_corruption"
    CANONICAL_STATE_MISMATCH = "canonical_state_mismatch"


class InjectionPoint(StrEnum):
    PROVIDER_REQUEST = "provider_request"
    LLM_REQUEST = "llm_request"
    MODEL_INFERENCE = "model_inference"
    SIDECAR_REQUEST = "sidecar_request"
    DATABASE_CONNECTION = "database_connection"
    WORKER_AFTER_RECEIPT_COMMIT = "worker_after_receipt_commit"
    LEASE_RENEWAL = "lease_renewal"
    EVENT_DELIVERY = "event_delivery"
    ARTIFACT_READ = "artifact_read"
    LEDGER_BEFORE_COMMIT = "ledger_before_commit"
    DATABASE_RESTORE = "database_restore"


class ExpectedDrillState(StrEnum):
    DEGRADED = "degraded"
    FAILED = "failed"
    REJECTED = "rejected"
    FENCED = "fenced"


class ForbiddenSideEffect(StrEnum):
    TARGET_CREATED = "target_created"
    RESERVATION_CREATED = "reservation_created"
    ORDER_CREATED = "order_created"
    DUPLICATE_RECEIPT_COMMITTED = "duplicate_receipt_committed"
    DUPLICATE_FILL_COMMITTED = "duplicate_fill_committed"
    DUPLICATE_JOURNAL_COMMITTED = "duplicate_journal_committed"
    STALE_RESULT_COMMITTED = "stale_result_committed"
    CORRUPT_ARTIFACT_REPLAYED = "corrupt_artifact_replayed"
    UNBALANCED_JOURNAL_COMMITTED = "unbalanced_journal_committed"
    EXISTING_FILL_DELETED = "existing_fill_deleted"
    EXISTING_JOURNAL_DELETED = "existing_journal_deleted"
    SOURCE_DATABASE_MUTATED = "source_database_mutated"


class RecoveryPrecondition(StrEnum):
    DEPENDENCY_HEALTHY = "dependency_healthy"
    DATABASE_HEALTHY = "database_healthy"
    FENCE_ADVANCED = "fence_advanced"
    DEAD_LETTER_INSPECTED = "dead_letter_inspected"
    INBOX_DEDUPLICATION_VERIFIED = "inbox_deduplication_verified"
    ARTIFACT_INTEGRITY_VERIFIED = "artifact_integrity_verified"
    KILL_SWITCH_ACTIVE = "kill_switch_active"
    LEDGER_BALANCED = "ledger_balanced"
    RECONCILIATION_PASSED = "reconciliation_passed"
    REPLAY_VERIFIED = "replay_verified"
    AUDIT_CHAIN_VERIFIED = "audit_chain_verified"
    FRESH_RESTORE_TARGET = "fresh_restore_target"
    SOURCE_TARGET_ISOLATED = "source_target_isolated"
    ALEMBIC_HEAD_VERIFIED = "alembic_head_verified"
    HASH_CHAIN_VERIFIED = "hash_chain_verified"
    APPEND_ONLY_VERIFIED = "append_only_verified"
    OPERATOR_APPROVED = "operator_approved"


class DrillVerificationReason(StrEnum):
    UNKNOWN_DRILL = "unknown_drill"
    CONTRACT_MISMATCH = "contract_mismatch"
    FORBIDDEN_SIDE_EFFECT = "forbidden_side_effect"
    EVIDENCE_INCOMPLETE = "evidence_incomplete"
    UNSAFE_RECOVERY = "unsafe_recovery"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MetricLabel(_FrozenModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    value: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")


class MetricEvidence(_FrozenModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_:]{0,127}$")
    labels: tuple[MetricLabel, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_labels(self) -> Self:
        _require_unique(tuple(item.name for item in self.labels), "metric labels")
        validate_metric_measurement(
            self.name,
            MetricKind.COUNTER,
            1,
            {item.name: item.value for item in self.labels},
        )
        return self


class DrillDefinition(_FrozenModel):
    drill_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    failure_class: FailureClass
    injection_point: InjectionPoint
    expected_state: ExpectedDrillState
    forbidden_side_effects: tuple[ForbiddenSideEffect, ...] = Field(
        min_length=1, max_length=12
    )
    required_audit_evidence: tuple[str, ...] = Field(min_length=1, max_length=8)
    required_metric_evidence: tuple[MetricEvidence, ...] = Field(
        min_length=1, max_length=8
    )
    recovery_preconditions: tuple[RecoveryPrecondition, ...] = Field(
        min_length=1, max_length=16
    )

    @model_validator(mode="after")
    def validate_closed_sets(self) -> Self:
        _require_identifiers(self.required_audit_evidence, "audit evidence")
        _require_unique(self.forbidden_side_effects, "forbidden side effects")
        _require_unique(self.required_audit_evidence, "audit evidence")
        _require_unique(self.required_metric_evidence, "metric evidence")
        _require_unique(self.recovery_preconditions, "recovery preconditions")
        return self


class MeasurementContract(_FrozenModel):
    rto_start: Literal["injected_at"]
    rto_end: Literal["recovered_at"]
    rpo_source: Literal["last_durable_commit_at"]
    rpo_recovered: Literal["recovered_through_at"]
    unit: Literal["seconds"]
    production_sla_claim: Literal[False]


class FailClosedPolicy(_FrozenModel):
    unknown_drill: Literal["reject"]
    duplicate_entry: Literal["reject"]
    partial_result: Literal["reject"]
    unsafe_recovery: Literal["reject"]
    automatic_recovery: Literal[False]


class ResilienceDrillPolicy(_FrozenModel):
    schema_version: Literal[1]
    policy_id: Literal["stonks-resilience-drills/1"]
    execution_mode: Literal["paper"]
    fail_closed: FailClosedPolicy
    measurement_contract: MeasurementContract
    drills: tuple[DrillDefinition, ...] = Field(min_length=11, max_length=11)

    @model_validator(mode="after")
    def validate_closed_catalog(self) -> Self:
        actual = tuple(item.drill_id for item in self.drills)
        if actual != DRILL_CATALOG:
            raise ValueError("drill catalog is incomplete, duplicated, or reordered")
        _require_unique(
            tuple(item.injection_point for item in self.drills), "injection points"
        )
        return self

    def definition_for(self, drill_id: str) -> DrillDefinition | None:
        return next((item for item in self.drills if item.drill_id == drill_id), None)


class RecoveryMeasurement(_FrozenModel):
    injected_at: AwareDatetime
    recovered_at: AwareDatetime
    last_durable_commit_at: AwareDatetime
    recovered_through_at: AwareDatetime
    measured_rto_seconds: Decimal = Field(ge=0)
    measured_rpo_seconds: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def validate_exact_measurements(self) -> Self:
        instants = (
            self.injected_at,
            self.recovered_at,
            self.last_durable_commit_at,
            self.recovered_through_at,
        )
        if any(item.utcoffset() != timedelta(0) for item in instants):
            raise ValueError("drill measurements must use UTC")
        rto = _seconds_between(self.injected_at, self.recovered_at)
        rpo = _seconds_between(self.recovered_through_at, self.last_durable_commit_at)
        source_age = _seconds_between(self.last_durable_commit_at, self.injected_at)
        if rto < 0 or rpo < 0 or source_age < 0:
            raise ValueError("drill measurement instants are unsafe")
        if self.measured_rto_seconds != rto or self.measured_rpo_seconds != rpo:
            raise ValueError("RTO/RPO measured fields do not match their instants")
        return self


class DrillReport(_FrozenModel):
    schema_version: Literal[1]
    policy_id: Literal["stonks-resilience-drills/1"]
    report_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    drill_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    outcome: Literal["complete"]
    failure_class: FailureClass
    injection_point: InjectionPoint
    observed_state: ExpectedDrillState
    forbidden_side_effects_observed: tuple[ForbiddenSideEffect, ...] = ()
    audit_evidence: tuple[str, ...] = ()
    metric_evidence: tuple[MetricEvidence, ...] = ()
    satisfied_recovery_preconditions: tuple[RecoveryPrecondition, ...] = ()
    recovery_authorized: Literal[True]
    measurement: RecoveryMeasurement

    @model_validator(mode="after")
    def validate_no_duplicate_claims(self) -> Self:
        _require_identifiers(self.audit_evidence, "audit evidence")
        _require_unique(self.audit_evidence, "audit evidence")
        _require_unique(self.metric_evidence, "metric evidence")
        _require_unique(self.satisfied_recovery_preconditions, "recovery preconditions")
        return self


class DrillVerificationError(RuntimeError):
    def __init__(self, reason: DrillVerificationReason) -> None:
        self.reason = reason
        super().__init__("Resilience drill report failed closed")


DRILL_CATALOG = (
    "provider_outage",
    "llm_outage",
    "model_outage",
    "sidecar_outage",
    "database_restart",
    "worker_crash",
    "lease_expiry",
    "duplicate_event",
    "artifact_corruption",
    "ledger_mismatch",
    "database_restore",
)


def verify_drill_report(
    policy: ResilienceDrillPolicy, report: DrillReport
) -> DrillReport:
    definition = policy.definition_for(report.drill_id)
    if definition is None:
        raise DrillVerificationError(DrillVerificationReason.UNKNOWN_DRILL)
    actual_contract = (
        report.failure_class,
        report.injection_point,
        report.observed_state,
    )
    expected_contract = (
        definition.failure_class,
        definition.injection_point,
        definition.expected_state,
    )
    if actual_contract != expected_contract:
        raise DrillVerificationError(DrillVerificationReason.CONTRACT_MISMATCH)
    if report.forbidden_side_effects_observed:
        raise DrillVerificationError(DrillVerificationReason.FORBIDDEN_SIDE_EFFECT)
    _verify_evidence(definition, report)
    return report


def _verify_evidence(definition: DrillDefinition, report: DrillReport) -> None:
    if (
        report.audit_evidence != definition.required_audit_evidence
        or report.metric_evidence != definition.required_metric_evidence
    ):
        raise DrillVerificationError(DrillVerificationReason.EVIDENCE_INCOMPLETE)
    if report.satisfied_recovery_preconditions != definition.recovery_preconditions:
        raise DrillVerificationError(DrillVerificationReason.UNSAFE_RECOVERY)


def _require_identifiers(values: tuple[str, ...], label: str) -> None:
    for value in values:
        if (
            not value
            or len(value) > 64
            or not value[0].isalpha()
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789_"
                for character in value
            )
        ):
            raise ValueError(f"{label} must use bounded identifiers")


def _require_unique(values: tuple[object, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must not be duplicated")


def _seconds_between(start: datetime, end: datetime) -> Decimal:
    delta = end - start
    whole_microseconds = (
        delta.days * 86_400 + delta.seconds
    ) * 1_000_000 + delta.microseconds
    return Decimal(whole_microseconds) / Decimal(1_000_000)
