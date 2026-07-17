"""Immutable production cost/latency budgets with monotonic outcomes."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from stonks_agent.domain.telemetry import (
    BudgetDimension,
    BudgetScope,
)
from stonks_agent.domain.telemetry import (
    BudgetOutcome as BudgetStatus,
)
from stonks_contracts.common import NonNegativeDecimal, PositiveDecimal

__all__ = [
    "BudgetDecision",
    "BudgetDecisionReason",
    "BudgetDimension",
    "BudgetScope",
    "BudgetStatus",
    "BudgetThreshold",
    "BudgetUsage",
    "BudgetViolation",
    "OperationalBudgetPolicy",
    "evaluate_budget",
]

MAX_COST_USD = Decimal("1000000")
MAX_ELAPSED_SECONDS = Decimal("86400")
MAX_MONOTONIC_SECONDS = Decimal("1000000000000")


class BudgetDecisionReason(StrEnum):
    WITHIN_BUDGET = "within_budget"
    THRESHOLD_EXCEEDED = "threshold_exceeded"
    USAGE_MISSING = "usage_missing"
    USAGE_INVALID = "usage_invalid"


class BudgetThreshold(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: BudgetStatus
    max_cost_usd: PositiveDecimal
    max_elapsed_seconds: PositiveDecimal

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.action is BudgetStatus.WITHIN:
            raise ValueError("budget threshold action must be degraded or failed")
        if self.max_cost_usd > MAX_COST_USD:
            raise ValueError("budget cost threshold exceeds supported bound")
        if self.max_elapsed_seconds > MAX_ELAPSED_SECONDS:
            raise ValueError("budget elapsed threshold exceeds supported bound")
        return self


class OperationalBudgetPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: BudgetScope
    degraded: BudgetThreshold
    failed: BudgetThreshold

    @model_validator(mode="after")
    def validate_threshold_order(self) -> Self:
        if self.degraded.action is not BudgetStatus.DEGRADED:
            raise ValueError("soft budget action must be degraded")
        if self.failed.action is not BudgetStatus.FAILED:
            raise ValueError("hard budget action must be failed")
        if (
            self.failed.max_cost_usd <= self.degraded.max_cost_usd
            or self.failed.max_elapsed_seconds <= self.degraded.max_elapsed_seconds
        ):
            raise ValueError("hard budget thresholds must exceed soft thresholds")
        return self


class BudgetUsage(BaseModel):
    """A cumulative cost and two readings from the same monotonic clock."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cost_usd: NonNegativeDecimal = Field(le=MAX_COST_USD)
    monotonic_started_seconds: NonNegativeDecimal = Field(le=MAX_MONOTONIC_SECONDS)
    monotonic_observed_seconds: NonNegativeDecimal = Field(le=MAX_MONOTONIC_SECONDS)

    @model_validator(mode="after")
    def validate_monotonic_readings(self) -> Self:
        if self.monotonic_observed_seconds < self.monotonic_started_seconds:
            raise ValueError("monotonic observation cannot precede its start")
        return self

    @property
    def elapsed_seconds(self) -> Decimal:
        return self.monotonic_observed_seconds - self.monotonic_started_seconds


class BudgetViolation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dimension: BudgetDimension
    action: BudgetStatus
    threshold: PositiveDecimal
    observed: NonNegativeDecimal

    @model_validator(mode="after")
    def validate_exceeded_threshold(self) -> Self:
        if self.action is BudgetStatus.WITHIN or self.observed <= self.threshold:
            raise ValueError("budget violation must exceed an actionable threshold")
        return self


class BudgetDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: BudgetScope
    status: BudgetStatus
    evaluated_status: BudgetStatus
    previous_status: BudgetStatus
    reason: BudgetDecisionReason
    usage: BudgetUsage | None
    violations: tuple[BudgetViolation, ...] = Field(max_length=2)

    @model_validator(mode="after")
    def validate_consistent_outcome(self) -> Self:
        expected = _strongest((self.previous_status, self.evaluated_status))
        if self.status is not expected:
            raise ValueError("budget status must preserve the strongest outcome")
        if self.usage is None:
            if (
                self.evaluated_status is not BudgetStatus.FAILED
                or self.violations
                or self.reason
                not in {
                    BudgetDecisionReason.USAGE_MISSING,
                    BudgetDecisionReason.USAGE_INVALID,
                }
            ):
                raise ValueError("invalid usage must produce a closed failed outcome")
            return self
        if len({item.dimension for item in self.violations}) != len(self.violations):
            raise ValueError("budget violation dimensions must be unique")
        evaluated = _strongest(
            tuple(item.action for item in self.violations) or (BudgetStatus.WITHIN,)
        )
        expected_reason = (
            BudgetDecisionReason.THRESHOLD_EXCEEDED
            if self.violations
            else BudgetDecisionReason.WITHIN_BUDGET
        )
        if self.evaluated_status is not evaluated or self.reason is not expected_reason:
            raise ValueError("budget decision does not match its violations")
        return self


_SEVERITY = {
    BudgetStatus.WITHIN: 0,
    BudgetStatus.DEGRADED: 1,
    BudgetStatus.FAILED: 2,
}


def evaluate_budget(
    policy: OperationalBudgetPolicy,
    usage: object,
    *,
    previous_status: BudgetStatus = BudgetStatus.WITHIN,
) -> BudgetDecision:
    """Evaluate usage while preserving the strongest prior terminal outcome."""

    observed, invalid_reason = _validated_usage(usage)
    if observed is None:
        return _invalid_usage_decision(policy, previous_status, invalid_reason)
    violations = _violations(policy, observed)
    evaluated = _strongest(
        tuple(item.action for item in violations) or (BudgetStatus.WITHIN,)
    )
    return BudgetDecision(
        scope=policy.scope,
        status=_strongest((previous_status, evaluated)),
        evaluated_status=evaluated,
        previous_status=previous_status,
        reason=(
            BudgetDecisionReason.THRESHOLD_EXCEEDED
            if violations
            else BudgetDecisionReason.WITHIN_BUDGET
        ),
        usage=observed,
        violations=violations,
    )


def _validated_usage(
    value: object,
) -> tuple[BudgetUsage | None, BudgetDecisionReason]:
    if value is None:
        return None, BudgetDecisionReason.USAGE_MISSING
    try:
        return BudgetUsage.model_validate(value), BudgetDecisionReason.WITHIN_BUDGET
    except ValidationError:
        return None, BudgetDecisionReason.USAGE_INVALID


def _invalid_usage_decision(
    policy: OperationalBudgetPolicy,
    previous_status: BudgetStatus,
    reason: BudgetDecisionReason,
) -> BudgetDecision:
    return BudgetDecision(
        scope=policy.scope,
        status=BudgetStatus.FAILED,
        evaluated_status=BudgetStatus.FAILED,
        previous_status=previous_status,
        reason=reason,
        usage=None,
        violations=(),
    )


def _violations(
    policy: OperationalBudgetPolicy,
    usage: BudgetUsage,
) -> tuple[BudgetViolation, ...]:
    values = (
        (
            BudgetDimension.COST,
            usage.cost_usd,
            policy.degraded.max_cost_usd,
            policy.failed.max_cost_usd,
        ),
        (
            BudgetDimension.LATENCY,
            usage.elapsed_seconds,
            policy.degraded.max_elapsed_seconds,
            policy.failed.max_elapsed_seconds,
        ),
    )
    return tuple(
        violation
        for dimension, observed, soft, hard in values
        if (violation := _violation(dimension, observed, soft, hard)) is not None
    )


def _violation(
    dimension: BudgetDimension,
    observed: Decimal,
    soft: Decimal,
    hard: Decimal,
) -> BudgetViolation | None:
    if observed > hard:
        return BudgetViolation(
            dimension=dimension,
            action=BudgetStatus.FAILED,
            threshold=hard,
            observed=observed,
        )
    if observed > soft:
        return BudgetViolation(
            dimension=dimension,
            action=BudgetStatus.DEGRADED,
            threshold=soft,
            observed=observed,
        )
    return None


def _strongest(statuses: tuple[BudgetStatus, ...]) -> BudgetStatus:
    return max(statuses, key=_SEVERITY.__getitem__)
