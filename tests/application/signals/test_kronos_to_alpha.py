from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from stonks_agent.application.signals.kronos_to_alpha import (
    KronosToAlphaCommand,
    load_kronos_strategy_configuration,
    map_kronos_to_alpha,
)
from stonks_agent.domain.errors import Failure, Success
from stonks_agent.domain.evaluation import (
    EvaluationCheck,
    EvaluationCheckKind,
    EvaluationCheckStatus,
    EvaluationMetric,
    EvaluationReport,
)
from stonks_agent.domain.signal import (
    ForecastOutputArtifact,
    SignalDirection,
    SignalSource,
    evaluate_signal_eligibility,
)
from stonks_agent.domain.strategy import (
    PromotionState,
    StrategyRegistryEntry,
)
from stonks_contracts.common import ConfidenceCalibration
from stonks_contracts.market_data import DataQuality, DataQualityStatus
from stonks_contracts.signal import ForecastSignal

ROOT = Path(__file__).resolve().parents[3]
CONFIGURATION_PATH = ROOT / "config" / "strategies" / "kronos.yaml"
MAPPER_PATH = (
    ROOT / "src" / "stonks_agent" / "application" / "signals" / "kronos_to_alpha.py"
)
NOW = datetime(2026, 7, 13, 3, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
CURRENT_SNAPSHOT_ID = UUID("10000000-0000-4000-8000-000000000830")
EVALUATION_SNAPSHOT_ID = UUID("10000000-0000-4000-8000-000000000831")


def _report(*, passed: bool = True, expired: bool = False) -> EvaluationReport:
    configuration = load_kronos_strategy_configuration(CONFIGURATION_PATH)
    failed_kind = EvaluationCheckKind.BASELINE_COMPARISON
    checks = tuple(
        EvaluationCheck(
            kind=kind,
            status=(
                EvaluationCheckStatus.PASSED
                if passed or kind is not failed_kind
                else EvaluationCheckStatus.FAILED
            ),
            reason_codes=()
            if passed or kind is not failed_kind
            else ("baseline_comparison_failed",),
        )
        for kind in EvaluationCheckKind
    )
    return EvaluationReport(
        report_id=UUID("10000000-0000-4000-8000-000000000832"),
        strategy_id=configuration.manifest.strategy_id,
        strategy_version=configuration.manifest.strategy_version,
        strategy_manifest_hash=configuration.manifest.manifest_hash,
        dataset_snapshot_id=EVALUATION_SNAPSHOT_ID,
        data_hash=HASH_A,
        runtime_hash=configuration.manifest.runtime_hash,
        evaluation_policy_hash=configuration.evaluation_policy_hash,
        as_of=NOW - timedelta(days=10),
        window_start=NOW - timedelta(days=100),
        window_end=NOW - timedelta(days=11),
        checks=checks,
        metrics=(
            EvaluationMetric(name="net_alpha", value=Decimal("0.001"), unit="return"),
        ),
        calibration=ConfidenceCalibration.CALIBRATED,
        baseline_ids=configuration.required_baselines,
        report_artifact_ref=f"sha256:{HASH_A}",
        valid_until=NOW - timedelta(seconds=1) if expired else NOW + timedelta(days=20),
        created_at=NOW - timedelta(days=9),
        passed=passed,
    )


def _registry(
    report: EvaluationReport,
    *,
    state: PromotionState = PromotionState.SHADOW,
) -> StrategyRegistryEntry:
    configuration = load_kronos_strategy_configuration(CONFIGURATION_PATH)
    return StrategyRegistryEntry(
        manifest=configuration.manifest,
        state=state,
        evaluation_report_id=report.report_id,
        evaluation_hash=report.evaluation_hash,
        version=3,
        created_at=NOW - timedelta(days=30),
        updated_at=NOW - timedelta(days=9),
    )


def _forecast_output(**changes: object) -> ForecastOutputArtifact:
    configuration = load_kronos_strategy_configuration(CONFIGURATION_PATH)
    forecast = ForecastSignal(
        forecast_id=UUID("10000000-0000-4000-8000-000000000833"),
        instrument_id=UUID("10000000-0000-4000-8000-000000000834"),
        as_of=NOW - timedelta(minutes=2),
        interval="1d",
        horizon_bars=1,
        expected_return=Decimal("0.30"),
        median_return=Decimal("0.25"),
        direction_probability=Decimal("0.80"),
        expected_volatility=Decimal("0.02"),
        downside_quantile=Decimal("-0.03"),
        max_drawdown_quantile=Decimal("-0.04"),
        path_count=16,
        dispersion=Decimal("0.01"),
        input_quality=DataQuality(
            status=DataQualityStatus.AVAILABLE,
            completeness=Decimal(1),
        ),
        model_id=configuration.model_id,
        model_revision=configuration.model_revision,
        tokenizer_id=configuration.tokenizer_id,
        tokenizer_revision=configuration.tokenizer_revision,
        device="cpu",
        seed_policy="explicit-sequential-v1",
        inference_code_version="kronos-path-retention/1.0.0",
        dataset_snapshot_id=CURRENT_SNAPSHOT_ID,
        input_window_start=NOW - timedelta(days=2),
        input_window_end=NOW - timedelta(minutes=3),
        generated_at=NOW - timedelta(minutes=1),
        latency_ms=10,
    )
    payload: dict[str, object] = {
        "request_id": UUID("10000000-0000-4000-8000-000000000835"),
        "forecast": forecast,
        "raw_output_artifact_ref": f"sha256:{HASH_A}",
        "sampled_paths_artifact_ref": f"sha256:{HASH_B}",
        "model_artifact_hash": configuration.model_artifact_hash,
        "tokenizer_artifact_hash": configuration.tokenizer_artifact_hash,
        "runtime_hash": configuration.manifest.runtime_hash,
        "data_hash": HASH_B,
        "stochastic": True,
        "created_at": NOW - timedelta(minutes=1),
    }
    return ForecastOutputArtifact.model_validate(payload | changes)


def _command(
    *,
    report: EvaluationReport | None = None,
    registry: StrategyRegistryEntry | None = None,
    forecast_output: ForecastOutputArtifact | None = None,
) -> KronosToAlphaCommand:
    evaluation = report or _report()
    return KronosToAlphaCommand(
        forecast_output=forecast_output or _forecast_output(),
        registry=registry or _registry(evaluation),
        evaluation=evaluation,
        generated_at=NOW,
    )


def test_committed_mapper_source_matches_manifest_artifact_hash() -> None:
    configuration = load_kronos_strategy_configuration(CONFIGURATION_PATH)
    source_hash = hashlib.sha256(MAPPER_PATH.read_bytes()).hexdigest()

    assert configuration.manifest.source_artifact_ref == f"sha256:{source_hash}"


def test_evaluated_shadow_forecast_maps_to_alpha_but_stays_zero_weight() -> None:
    configuration = load_kronos_strategy_configuration(CONFIGURATION_PATH)
    command = _command()

    first = map_kronos_to_alpha(command, configuration)
    second = map_kronos_to_alpha(command, configuration)

    assert isinstance(first, Success)
    assert first == second
    signal = first.value
    assert signal.value == Decimal("0.25")
    assert signal.confidence == Decimal("0.60")
    assert signal.direction is SignalDirection.LONG
    assert signal.source is SignalSource.FORECAST
    assert signal.dataset_snapshot_id == CURRENT_SNAPSHOT_ID
    assert signal.dataset_snapshot_id != EVALUATION_SNAPSHOT_ID
    decision = evaluate_signal_eligibility(
        signal,
        registry=command.registry,
        evaluation=command.evaluation,
        at=NOW,
    )
    assert decision.eligible is False
    assert decision.weight == Decimal(0)
    assert decision.reason_codes == ("strategy_not_paper_eligible",)


@pytest.mark.parametrize(
    "failure", ["state", "failed", "expired", "runtime", "model", "quality"]
)
def test_invalid_promotion_or_forecast_binding_produces_no_alpha(failure: str) -> None:
    configuration = load_kronos_strategy_configuration(CONFIGURATION_PATH)
    report = _report(passed=failure != "failed", expired=failure == "expired")
    registry = _registry(
        report,
        state=(
            PromotionState.PAPER_ELIGIBLE
            if failure == "state"
            else PromotionState.SHADOW
        ),
    )
    output = _forecast_output()
    if failure == "runtime":
        output = output.model_copy(update={"runtime_hash": HASH_A})
    if failure == "model":
        output = output.model_copy(update={"model_artifact_hash": HASH_A})
    if failure == "quality":
        forecast = output.forecast.model_copy(
            update={
                "input_quality": DataQuality(
                    status=DataQualityStatus.STALE,
                    completeness=Decimal(1),
                )
            }
        )
        output = output.model_copy(update={"forecast": forecast})

    result = map_kronos_to_alpha(
        _command(report=report, registry=registry, forecast_output=output),
        configuration,
    )

    assert isinstance(result, Failure)


def test_kronos_alpha_rejects_order_shaped_fields() -> None:
    result = map_kronos_to_alpha(
        _command(), load_kronos_strategy_configuration(CONFIGURATION_PATH)
    )
    assert isinstance(result, Success)

    for forbidden in ("quantity", "target_weight", "order_intent", "risk_override"):
        with pytest.raises(ValidationError, match="extra_forbidden"):
            type(result.value).model_validate(
                result.value.model_dump() | {forbidden: "100"}
            )
