"""Point-in-time strategy evaluation requests and immutable reports."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stonks_agent.domain.strategy import StrategyManifest
from stonks_contracts.common import (
    ArtifactRef,
    ConfidenceCalibration,
    DecimalString,
    Sha256,
    UTCDateTime,
    stable_payload_hash,
)


class EvaluationCheckKind(StrEnum):
    POINT_IN_TIME = "point_in_time"
    LEAKAGE = "leakage"
    SURVIVORSHIP = "survivorship"
    REPRODUCIBILITY = "reproducibility"
    BASELINE_COMPARISON = "baseline_comparison"
    COST_SENSITIVITY = "cost_sensitivity"


MANDATORY_EVALUATION_CHECKS = frozenset(EvaluationCheckKind)


class EvaluationCheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


class EvaluationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EvaluationCheckKind
    status: EvaluationCheckStatus
    reason_codes: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    details_artifact_ref: ArtifactRef | None = None

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            not value.strip() for value in values
        ):
            raise ValueError("evaluation reason codes must be unique and non-blank")
        return values


class EvaluationMetric(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    value: DecimalString
    unit: str = Field(pattern=r"^[a-z][a-z0-9_.%/-]{0,63}$")
    segment: str = Field(default="overall", pattern=r"^[a-z][a-z0-9_.-]{0,127}$")


class EvaluationRequest(BaseModel):
    """Artifact-only evaluation input suitable for an isolated strategy lab."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    manifest: StrategyManifest
    dataset_snapshot_id: UUID
    snapshot_artifact_ref: ArtifactRef
    data_hash: Sha256
    as_of: UTCDateTime
    window_start: UTCDateTime
    window_end: UTCDateTime
    evaluation_policy_hash: Sha256
    requested_at: UTCDateTime
    deadline_at: UTCDateTime

    @property
    def runtime_hash(self) -> str:
        return self.manifest.runtime_hash

    @model_validator(mode="after")
    def validate_timeline(self) -> Self:
        if self.window_end <= self.window_start:
            raise ValueError("evaluation window end must be later than start")
        if self.window_end > self.as_of:
            raise ValueError("evaluation window cannot extend beyond as_of")
        if self.requested_at < self.as_of:
            raise ValueError("evaluation request cannot precede its as_of")
        if self.deadline_at <= self.requested_at:
            raise ValueError("evaluation deadline must be later than request time")
        return self


class EvaluationReport(BaseModel):
    """Evaluation truth bound to exact strategy, data, runtime, and policy hashes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    report_id: UUID
    strategy_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    strategy_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    strategy_manifest_hash: Sha256
    dataset_snapshot_id: UUID
    data_hash: Sha256
    runtime_hash: Sha256
    evaluation_policy_hash: Sha256
    as_of: UTCDateTime
    window_start: UTCDateTime
    window_end: UTCDateTime
    checks: tuple[EvaluationCheck, ...] = Field(min_length=1, max_length=64)
    metrics: tuple[EvaluationMetric, ...] = Field(min_length=1, max_length=512)
    calibration: ConfidenceCalibration
    baseline_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    report_artifact_ref: ArtifactRef
    valid_until: UTCDateTime
    created_at: UTCDateTime
    passed: bool

    @field_validator("checks")
    @classmethod
    def normalize_checks(
        cls, values: tuple[EvaluationCheck, ...]
    ) -> tuple[EvaluationCheck, ...]:
        kinds = tuple(value.kind for value in values)
        if len(kinds) != len(set(kinds)):
            raise ValueError("evaluation checks must be unique")
        return tuple(sorted(values, key=lambda value: value.kind.value))

    @field_validator("metrics")
    @classmethod
    def normalize_metrics(
        cls, values: tuple[EvaluationMetric, ...]
    ) -> tuple[EvaluationMetric, ...]:
        keys = tuple((value.name, value.segment) for value in values)
        if len(keys) != len(set(keys)):
            raise ValueError("evaluation metrics must be unique per segment")
        return tuple(sorted(values, key=lambda value: (value.name, value.segment)))

    @field_validator("baseline_ids")
    @classmethod
    def normalize_baselines(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            not value.strip() for value in values
        ):
            raise ValueError("baseline ids must be unique and non-blank")
        return tuple(sorted(values))

    @model_validator(mode="after")
    def validate_integrity(self) -> Self:
        present = frozenset(check.kind for check in self.checks)
        if not present >= MANDATORY_EVALUATION_CHECKS:
            raise ValueError("mandatory evaluation checks are missing")
        required_passed = all(
            check.status is EvaluationCheckStatus.PASSED
            for check in self.checks
            if check.kind in MANDATORY_EVALUATION_CHECKS
        )
        if self.passed and (
            not required_passed
            or self.calibration is not ConfidenceCalibration.CALIBRATED
        ):
            raise ValueError("passed evaluation requires passed checks and calibration")
        if self.window_end <= self.window_start or self.window_end > self.as_of:
            raise ValueError("evaluation report has an invalid point-in-time window")
        if self.created_at < self.as_of or self.valid_until <= self.created_at:
            raise ValueError("evaluation report has an invalid validity timeline")
        return self

    @property
    def evaluation_hash(self) -> str:
        payload = self.model_dump(
            mode="json",
            exclude={"report_id", "report_artifact_ref", "created_at", "valid_until"},
        )
        return stable_payload_hash(payload)


def metric_map(report: EvaluationReport) -> dict[tuple[str, str], Decimal]:
    return {(metric.name, metric.segment): metric.value for metric in report.metrics}
