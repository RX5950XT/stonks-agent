from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from stonks_agent.config.resilience import load_resilience_drill_policy
from stonks_agent.domain.resilience import (
    DrillReport,
    DrillVerificationError,
    ExpectedDrillState,
    FailureClass,
    ForbiddenSideEffect,
    InjectionPoint,
    RecoveryMeasurement,
    verify_drill_report,
)

POLICY = load_resilience_drill_policy(Path("config/resilience-drills.yaml"))
DEFINITION = POLICY.drills[0]
START = datetime(2026, 7, 22, 1, 0, tzinfo=UTC)


def _measurement(**changes: object) -> RecoveryMeasurement:
    values: dict[str, object] = {
        "injected_at": START,
        "recovered_at": START + timedelta(seconds=75),
        "last_durable_commit_at": START - timedelta(seconds=1),
        "recovered_through_at": START - timedelta(seconds=3),
        "measured_rto_seconds": Decimal("75"),
        "measured_rpo_seconds": Decimal("2"),
    }
    values.update(changes)
    return RecoveryMeasurement.model_validate(values)


def _report(**changes: object) -> DrillReport:
    values: dict[str, object] = {
        "schema_version": 1,
        "policy_id": POLICY.policy_id,
        "report_id": "provider_outage_20260722t010000z",
        "drill_id": DEFINITION.drill_id,
        "outcome": "complete",
        "failure_class": DEFINITION.failure_class,
        "injection_point": DEFINITION.injection_point,
        "observed_state": DEFINITION.expected_state,
        "forbidden_side_effects_observed": (),
        "audit_evidence": DEFINITION.required_audit_evidence,
        "metric_evidence": DEFINITION.required_metric_evidence,
        "satisfied_recovery_preconditions": DEFINITION.recovery_preconditions,
        "recovery_authorized": True,
        "measurement": _measurement(),
    }
    values.update(changes)
    return DrillReport.model_validate(values)


def test_complete_report_is_verified_without_mutation() -> None:
    report = _report()

    assert verify_drill_report(POLICY, report) is report
    with pytest.raises(Exception, match="frozen"):
        report.outcome = "partial"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"drill_id": "unknown_drill"}, "unknown_drill"),
        ({"failure_class": FailureClass.WORKER_FAILURE}, "contract_mismatch"),
        ({"injection_point": InjectionPoint.EVENT_DELIVERY}, "contract_mismatch"),
        ({"observed_state": ExpectedDrillState.FAILED}, "contract_mismatch"),
        (
            {"forbidden_side_effects_observed": (ForbiddenSideEffect.ORDER_CREATED,)},
            "forbidden_side_effect",
        ),
        ({"audit_evidence": ("provider_outage_detected",)}, "evidence_incomplete"),
        ({"metric_evidence": ()}, "evidence_incomplete"),
        (
            {"satisfied_recovery_preconditions": ("dependency_healthy",)},
            "unsafe_recovery",
        ),
    ],
)
def test_unknown_mismatched_partial_or_unsafe_report_fails_closed(
    changes: dict[str, object],
    reason: str,
) -> None:
    report = _report(**changes)

    with pytest.raises(DrillVerificationError) as raised:
        verify_drill_report(POLICY, report)

    assert raised.value.reason.value == reason


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("outcome", "partial"),
        ("recovery_authorized", False),
        (
            "audit_evidence",
            ("provider_outage_detected", "provider_outage_detected"),
        ),
        (
            "satisfied_recovery_preconditions",
            ("dependency_healthy", "dependency_healthy"),
        ),
    ],
)
def test_report_contract_rejects_partial_unsafe_or_duplicate_fields(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        _report(**{field: value})


@pytest.mark.parametrize(
    "changes",
    [
        {"recovered_at": START - timedelta(seconds=1)},
        {"recovered_through_at": START + timedelta(seconds=1)},
        {"measured_rto_seconds": Decimal("74")},
        {"measured_rpo_seconds": Decimal("3")},
        {"injected_at": START.replace(tzinfo=None)},
        {"injected_at": START.astimezone(timezone(timedelta(hours=8)))},
        {"last_durable_commit_at": START + timedelta(seconds=1)},
    ],
)
def test_rto_rpo_measurements_must_be_complete_utc_and_exact(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _measurement(**changes)


def test_report_rejects_noncanonical_audit_identifier() -> None:
    with pytest.raises(ValidationError, match="bounded identifiers"):
        _report(audit_evidence=("Provider outage",))
