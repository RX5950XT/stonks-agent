from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from stonks_agent.domain.evaluation import (
    MANDATORY_EVALUATION_CHECKS,
    EvaluationCheck,
    EvaluationCheckKind,
    EvaluationCheckStatus,
    EvaluationMetric,
    EvaluationReport,
)
from stonks_agent.domain.signal import (
    AlphaSignal,
    SignalDirection,
    SignalSource,
    evaluate_signal_eligibility,
)
from stonks_agent.domain.strategy import (
    PromotionState,
    StrategyKind,
    StrategyManifest,
    StrategyRegistryEntry,
    StrategyTransitionRequest,
    can_transition,
)
from stonks_contracts.common import ConfidenceCalibration

NOW = datetime(2026, 7, 13, 2, tzinfo=UTC)
STRATEGY_ID = "kronos-return"
STRATEGY_VERSION = "1.0.0"
MANIFEST_ID = UUID("00000000-0000-4000-8000-000000000101")
REPORT_ID = UUID("00000000-0000-4000-8000-000000000102")
SNAPSHOT_ID = UUID("00000000-0000-4000-8000-000000000103")
SIGNAL_ID = UUID("00000000-0000-4000-8000-000000000104")
INSTRUMENT_ID = UUID("00000000-0000-4000-8000-000000000105")
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HASH_F = "f" * 64


def manifest() -> StrategyManifest:
    return StrategyManifest(
        manifest_id=MANIFEST_ID,
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        kind=StrategyKind.FORECAST_MAPPER,
        source_artifact_ref=f"sha256:{HASH_A}",
        runtime_hash=HASH_B,
        feature_spec_hash=HASH_C,
        label_spec_hash=HASH_D,
        universe_spec_hash=HASH_E,
        cost_model_hash=HASH_F,
        split_policy_hash=HASH_A,
        parameters_hash=HASH_B,
        owner="quant-research",
        deterministic=False,
        created_at=NOW,
    )


def report(*, passed: bool = True) -> EvaluationReport:
    status = EvaluationCheckStatus.PASSED if passed else EvaluationCheckStatus.FAILED
    checks = tuple(
        EvaluationCheck(kind=kind, status=status)
        for kind in sorted(MANDATORY_EVALUATION_CHECKS, key=str)
    )
    return EvaluationReport(
        report_id=REPORT_ID,
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        strategy_manifest_hash=manifest().manifest_hash,
        dataset_snapshot_id=SNAPSHOT_ID,
        data_hash=HASH_C,
        runtime_hash=HASH_B,
        evaluation_policy_hash=HASH_E,
        as_of=NOW,
        window_start=NOW - timedelta(days=365),
        window_end=NOW - timedelta(days=1),
        checks=checks,
        metrics=(
            EvaluationMetric(name="net_alpha", value=Decimal("0.01"), unit="return"),
        ),
        calibration=ConfidenceCalibration.CALIBRATED,
        baseline_ids=("last-value/1.0.0",),
        report_artifact_ref=f"sha256:{HASH_D}",
        valid_until=NOW + timedelta(days=90),
        created_at=NOW,
        passed=passed,
    )


def registry(value: EvaluationReport) -> StrategyRegistryEntry:
    return StrategyRegistryEntry(
        manifest=manifest(),
        state=PromotionState.PAPER_ELIGIBLE,
        evaluation_report_id=value.report_id,
        evaluation_hash=value.evaluation_hash,
        version=4,
        created_at=NOW,
        updated_at=NOW,
    )


def signal(**overrides: object) -> AlphaSignal:
    value: dict[str, object] = {
        "signal_id": SIGNAL_ID,
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "instrument_id": INSTRUMENT_ID,
        "as_of": NOW,
        "generated_at": NOW + timedelta(seconds=1),
        "stale_at": NOW + timedelta(hours=1),
        "expires_at": NOW + timedelta(hours=2),
        "horizon": "5 sessions",
        "value": Decimal("0.4"),
        "confidence": Decimal("0.7"),
        "calibration": ConfidenceCalibration.CALIBRATED,
        "direction": SignalDirection.LONG,
        "source": SignalSource.FORECAST,
        "strategy_manifest_hash": manifest().manifest_hash,
        "dataset_snapshot_id": SNAPSHOT_ID,
        "data_hash": HASH_C,
        "runtime_hash": HASH_B,
        "evaluation_policy_hash": HASH_E,
        "raw_output_artifact_ref": f"sha256:{HASH_E}",
        "evaluation_report_id": REPORT_ID,
        "evaluation_hash": report().evaluation_hash,
        "forecast_refs": (UUID("00000000-0000-4000-8000-000000000106"),),
    }
    return AlphaSignal.model_validate(value | overrides)


@given(current=st.sampled_from(list(PromotionState)))
def test_promotion_transition_allowlist_never_accepts_self_transition(
    current: PromotionState,
) -> None:
    assert not can_transition(current, current)


def test_promotion_transition_graph_is_fixed_and_has_no_live_state() -> None:
    allowed = {
        (PromotionState.DRAFT, PromotionState.EVALUATING),
        (PromotionState.EVALUATING, PromotionState.REJECTED),
        (PromotionState.EVALUATING, PromotionState.SHADOW),
        (PromotionState.SHADOW, PromotionState.PAPER_ELIGIBLE),
        (PromotionState.PAPER_ELIGIBLE, PromotionState.SUSPENDED),
        (PromotionState.PAPER_ELIGIBLE, PromotionState.RETIRED),
        (PromotionState.SUSPENDED, PromotionState.EVALUATING),
        (PromotionState.SUSPENDED, PromotionState.RETIRED),
    }
    actual = {
        (current, target)
        for current in PromotionState
        for target in PromotionState
        if can_transition(current, target)
    }

    assert actual == allowed
    assert "live" not in {state.value for state in PromotionState}


def test_manifest_is_frozen_and_hashes_its_deterministic_identity() -> None:
    value = manifest()
    same = StrategyManifest.model_validate(value.model_dump())

    assert value.manifest_hash == same.manifest_hash
    with pytest.raises(ValidationError):
        value.owner = "mutated"  # type: ignore[misc]


def test_registry_requires_atomic_evaluation_binding_and_valid_timeline() -> None:
    base = registry(report()).model_dump()
    for overrides in (
        {"evaluation_hash": None},
        {
            "state": PromotionState.SHADOW,
            "evaluation_report_id": None,
            "evaluation_hash": None,
        },
        {"updated_at": NOW - timedelta(seconds=1)},
    ):
        with pytest.raises(ValidationError):
            StrategyRegistryEntry.model_validate(base | overrides)


def test_transition_request_enforces_graph_and_evaluation_gate() -> None:
    base = {
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "expected_version": 2,
        "current_state": PromotionState.EVALUATING,
        "target_state": PromotionState.SHADOW,
        "evaluation_report_id": REPORT_ID,
        "evaluation_hash": report().evaluation_hash,
        "reason_code": "evaluation_passed",
        "actor": "reviewer:test",
        "requested_at": NOW,
    }
    value = StrategyTransitionRequest.model_validate(base)

    assert value.target_state is PromotionState.SHADOW
    for overrides in (
        {"target_state": PromotionState.PAPER_ELIGIBLE},
        {"evaluation_hash": None},
        {"evaluation_report_id": None, "evaluation_hash": None},
    ):
        with pytest.raises(ValidationError):
            StrategyTransitionRequest.model_validate(base | overrides)


def test_evaluation_cannot_pass_without_every_mandatory_check() -> None:
    payload = report().model_dump(exclude={"evaluation_hash"})
    payload["checks"] = payload["checks"][:-1]

    with pytest.raises(ValidationError, match="mandatory evaluation checks"):
        EvaluationReport.model_validate(payload)


def test_evaluation_cannot_claim_passed_when_uncalibrated_or_failed() -> None:
    payload = report().model_dump(exclude={"evaluation_hash"})

    with pytest.raises(ValidationError, match="passed evaluation"):
        EvaluationReport.model_validate(
            payload | {"calibration": ConfidenceCalibration.UNCALIBRATED}
        )
    failed_check = EvaluationCheck(
        kind=EvaluationCheckKind.POINT_IN_TIME,
        status=EvaluationCheckStatus.FAILED,
    )
    checks = tuple(
        failed_check if value["kind"] == EvaluationCheckKind.POINT_IN_TIME else value
        for value in payload["checks"]
    )
    with pytest.raises(ValidationError, match="passed evaluation"):
        EvaluationReport.model_validate(payload | {"checks": checks})


@pytest.mark.parametrize(
    ("signal_overrides", "registry_state", "report_change", "at", "reason"),
    [
        (
            {"calibration": ConfidenceCalibration.UNCALIBRATED},
            None,
            None,
            None,
            "uncalibrated",
        ),
        ({}, PromotionState.SHADOW, None, None, "strategy_not_paper_eligible"),
        ({}, None, {"passed": False}, None, "evaluation_not_passed"),
        ({}, None, None, NOW + timedelta(hours=1), "signal_stale"),
        ({}, None, None, NOW + timedelta(hours=2), "signal_expired"),
        ({"evaluation_hash": HASH_F}, None, None, None, "evaluation_binding_mismatch"),
        ({"runtime_hash": HASH_F}, None, None, None, "strategy_binding_mismatch"),
    ],
)
def test_invalid_or_unregistered_signal_always_gets_zero_weight(
    signal_overrides: dict[str, object],
    registry_state: PromotionState | None,
    report_change: dict[str, object] | None,
    at: datetime | None,
    reason: str,
) -> None:
    evaluation = report()
    if report_change:
        evaluation = report(passed=bool(report_change["passed"]))
    entry = registry(evaluation)
    if registry_state is not None:
        entry = entry.model_copy(update={"state": registry_state})

    decision = evaluate_signal_eligibility(
        signal(**signal_overrides),
        registry=entry,
        evaluation=evaluation,
        at=at or NOW + timedelta(minutes=1),
    )

    assert decision.weight == Decimal(0)
    assert reason in decision.reason_codes


def test_exactly_bound_fresh_calibrated_signal_uses_calibrated_confidence() -> None:
    evaluation = report()

    decision = evaluate_signal_eligibility(
        signal(),
        registry=registry(evaluation),
        evaluation=evaluation,
        at=NOW + timedelta(minutes=1),
    )

    assert decision.eligible is True
    assert decision.weight == Decimal("0.7")
    assert decision.reason_codes == ("eligible",)


def test_eligible_zero_confidence_signal_remains_zero_without_becoming_invalid() -> (
    None
):
    evaluation = report()

    decision = evaluate_signal_eligibility(
        signal(
            value=Decimal(0),
            confidence=Decimal(0),
            direction=SignalDirection.NEUTRAL,
        ),
        registry=registry(evaluation),
        evaluation=evaluation,
        at=NOW + timedelta(minutes=1),
    )

    assert decision.eligible is True
    assert decision.weight == Decimal(0)


def test_signal_and_manifest_reject_order_shaped_fields() -> None:
    forbidden = ("target_weight", "quantity", "order_intent", "risk_override")
    for field in forbidden:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            AlphaSignal.model_validate(signal().model_dump() | {field: "1"})
        with pytest.raises(ValidationError, match="extra_forbidden"):
            StrategyManifest.model_validate(manifest().model_dump() | {field: "1"})


@pytest.mark.parametrize(
    ("value", "direction"),
    [
        (Decimal("-0.1"), SignalDirection.LONG),
        (Decimal("0.1"), SignalDirection.SHORT),
        (Decimal("0.1"), SignalDirection.NEUTRAL),
    ],
)
def test_signal_direction_must_match_its_signed_value(
    value: Decimal,
    direction: SignalDirection,
) -> None:
    with pytest.raises(ValidationError, match="direction"):
        signal(value=value, direction=direction)
