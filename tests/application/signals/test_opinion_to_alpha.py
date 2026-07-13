from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from stonks_agent.application.signals.opinion_to_alpha import (
    OpinionToAlphaCommand,
    OpinionToAlphaPolicy,
    load_opinion_mapper_policy,
    map_opinion_to_alpha,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.evaluation import (
    MANDATORY_EVALUATION_CHECKS,
    EvaluationCheck,
    EvaluationCheckStatus,
    EvaluationMetric,
    EvaluationReport,
)
from stonks_agent.domain.signal import SignalSource, evaluate_signal_eligibility
from stonks_agent.domain.strategy import (
    PromotionState,
    StrategyKind,
    StrategyManifest,
    StrategyRegistryEntry,
)
from stonks_contracts.common import ConfidenceCalibration
from stonks_contracts.research import AgentOpinion

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 7, 13, 2, tzinfo=UTC)
OPINION_ID = UUID("00000000-0000-4000-8000-000000000601")
INSTRUMENT_ID = UUID("00000000-0000-4000-8000-000000000602")
REPORT_ID = UUID("00000000-0000-4000-8000-000000000603")
EVALUATION_SNAPSHOT_ID = UUID("00000000-0000-4000-8000-000000000604")
CURRENT_SNAPSHOT_ID = UUID("00000000-0000-4000-8000-000000000605")
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HASH_F = "f" * 64


def configured_policy(*, enabled: bool = True) -> OpinionToAlphaPolicy:
    value = load_opinion_mapper_policy(
        ROOT / "config" / "policies" / "opinion_mappers.yaml"
    )
    return value.model_copy(update={"enabled": enabled})


def opinion(**changes: object) -> AgentOpinion:
    value: dict[str, object] = {
        "opinion_id": OPINION_ID,
        "instrument_id": INSTRUMENT_ID,
        "as_of": NOW,
        "horizon": "5d",
        "recommendation": "bullish",
        "thesis": "Evidence-backed margins improved.",
        "confidence": Decimal("0.7"),
        "calibration": ConfidenceCalibration.CALIBRATED,
        "evidence_refs": (UUID("00000000-0000-4000-8000-000000000606"),),
        "producer": "tradingagents",
        "model_version": "worker/1.0.0",
    }
    return AgentOpinion.model_validate(value | changes)


def manifest(policy: OpinionToAlphaPolicy) -> StrategyManifest:
    return StrategyManifest(
        manifest_id=UUID("00000000-0000-4000-8000-000000000607"),
        strategy_id=policy.strategy_id,
        strategy_version=policy.strategy_version,
        kind=StrategyKind.OPINION_MAPPER,
        source_artifact_ref=f"sha256:{HASH_A}",
        runtime_hash=HASH_B,
        feature_spec_hash=HASH_C,
        label_spec_hash=HASH_D,
        universe_spec_hash=HASH_E,
        cost_model_hash=HASH_F,
        split_policy_hash=HASH_A,
        parameters_hash=policy.policy_hash,
        owner="quant-research",
        deterministic=True,
        created_at=NOW,
    )


def evaluation(
    policy: OpinionToAlphaPolicy, *, passed: bool = True
) -> EvaluationReport:
    status = EvaluationCheckStatus.PASSED if passed else EvaluationCheckStatus.FAILED
    return EvaluationReport(
        report_id=REPORT_ID,
        strategy_id=policy.strategy_id,
        strategy_version=policy.strategy_version,
        strategy_manifest_hash=manifest(policy).manifest_hash,
        dataset_snapshot_id=EVALUATION_SNAPSHOT_ID,
        data_hash=HASH_C,
        runtime_hash=HASH_B,
        evaluation_policy_hash=HASH_D,
        as_of=NOW,
        window_start=NOW - timedelta(days=365),
        window_end=NOW - timedelta(days=1),
        checks=tuple(
            EvaluationCheck(kind=kind, status=status)
            for kind in MANDATORY_EVALUATION_CHECKS
        ),
        metrics=(EvaluationMetric(name="net_alpha", value="0.01", unit="return"),),
        calibration=ConfidenceCalibration.CALIBRATED,
        baseline_ids=("baseline-last-value/1.0.0",),
        report_artifact_ref=f"sha256:{HASH_E}",
        valid_until=NOW + timedelta(days=30),
        created_at=NOW,
        passed=passed,
    )


def registry(
    policy: OpinionToAlphaPolicy,
    report: EvaluationReport,
    *,
    state: PromotionState = PromotionState.PAPER_ELIGIBLE,
) -> StrategyRegistryEntry:
    return StrategyRegistryEntry(
        manifest=manifest(policy),
        state=state,
        evaluation_report_id=report.report_id,
        evaluation_hash=report.evaluation_hash,
        version=4,
        created_at=NOW,
        updated_at=NOW,
    )


def command(
    policy: OpinionToAlphaPolicy,
    **changes: object,
) -> OpinionToAlphaCommand:
    report = evaluation(policy)
    payload: dict[str, object] = {
        "opinion": opinion(),
        "registry": registry(policy, report),
        "evaluation": report,
        "dataset_snapshot_id": CURRENT_SNAPSHOT_ID,
        "data_hash": HASH_F,
        "raw_output_artifact_ref": f"sha256:{HASH_A}",
        "generated_at": NOW + timedelta(seconds=1),
    }
    return OpinionToAlphaCommand.model_validate(payload | changes)


def test_committed_mapper_policy_is_disabled_by_default() -> None:
    policy = configured_policy(enabled=False)

    result = map_opinion_to_alpha(command(policy), policy)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CAPABILITY_DENIED


@pytest.mark.parametrize(
    ("opinion_change", "state", "passed", "expected_code"),
    [
        (
            {"calibration": ConfidenceCalibration.UNCALIBRATED},
            PromotionState.PAPER_ELIGIBLE,
            True,
            ErrorCode.INVALID_INPUT,
        ),
        ({}, PromotionState.SHADOW, True, ErrorCode.CONFLICT),
        ({}, PromotionState.PAPER_ELIGIBLE, False, ErrorCode.CONFLICT),
        (
            {"recommendation": "strong_buy_100_shares"},
            PromotionState.PAPER_ELIGIBLE,
            True,
            ErrorCode.INVALID_INPUT,
        ),
    ],
)
def test_invalid_opinion_or_promotion_binding_produces_no_signal(
    opinion_change: dict[str, object],
    state: PromotionState,
    passed: bool,
    expected_code: ErrorCode,
) -> None:
    policy = configured_policy()
    report = evaluation(policy, passed=passed)
    value = command(
        policy,
        opinion=opinion(**opinion_change),
        registry=registry(policy, report, state=state),
        evaluation=report,
    )

    result = map_opinion_to_alpha(value, policy)

    assert isinstance(result, Failure)
    assert result.error.code is expected_code


def test_expired_mapper_evaluation_produces_no_signal() -> None:
    policy = configured_policy()
    report = evaluation(policy).model_copy(
        update={"valid_until": NOW + timedelta(milliseconds=500)}
    )
    value = command(
        policy,
        registry=registry(policy, report),
        evaluation=report,
    )

    result = map_opinion_to_alpha(value, policy)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT


@pytest.mark.parametrize(
    ("recommendation", "expected_value"),
    [
        ("bullish", Decimal("0.5")),
        ("neutral", Decimal(0)),
        ("bearish", Decimal("-0.5")),
    ],
)
def test_enabled_evaluated_mapper_produces_fixed_provenance_complete_alpha(
    recommendation: str,
    expected_value: Decimal,
) -> None:
    policy = configured_policy()
    report = evaluation(policy)
    value = command(policy, opinion=opinion(recommendation=recommendation))

    first = map_opinion_to_alpha(value, policy)
    second = map_opinion_to_alpha(value, policy)

    assert isinstance(first, Success)
    assert first == second
    signal = first.value
    assert signal.value == expected_value
    assert signal.confidence == Decimal("0.7")
    assert signal.source is SignalSource.OPINION
    assert signal.dataset_snapshot_id == CURRENT_SNAPSHOT_ID
    assert signal.dataset_snapshot_id != report.dataset_snapshot_id
    decision = evaluate_signal_eligibility(
        signal,
        registry=value.registry,
        evaluation=value.evaluation,
        at=value.generated_at,
    )
    assert decision.eligible is True
    assert decision.weight == Decimal("0.7")


def test_alpha_rejects_order_shaped_mapper_output() -> None:
    policy = configured_policy()
    result = map_opinion_to_alpha(command(policy), policy)
    assert isinstance(result, Success)

    for forbidden in ("quantity", "target_weight", "order_intent", "risk_override"):
        with pytest.raises(ValidationError, match="extra_forbidden"):
            type(result.value).model_validate(
                result.value.model_dump() | {forbidden: "100"}
            )
