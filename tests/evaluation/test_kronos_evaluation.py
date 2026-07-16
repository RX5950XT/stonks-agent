from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from pydantic import ValidationError

from stonks_agent.adapters.forecast.kronos import load_kronos_worker_configuration
from stonks_agent.application.evaluation.contracts import load_evaluation_policy
from stonks_agent.application.evaluation.kronos import (
    KronosBaselinePrediction,
    KronosEvaluationRecord,
    KronosEvaluationSnapshot,
    build_kronos_evaluation_inputs,
    evaluate_kronos_snapshot,
)
from stonks_agent.application.signals.kronos_to_alpha import (
    load_kronos_strategy_configuration,
)
from stonks_agent.domain.errors import Failure, Success
from stonks_agent.domain.evaluation import EvaluationCheckStatus, EvaluationRequest
from stonks_agent.domain.signal import ForecastOutputArtifact
from stonks_contracts.market_data import DataQuality, DataQualityStatus
from stonks_contracts.signal import ForecastSignal

ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "config" / "policies" / "evaluation_v1.yaml"
STRATEGY_PATH = ROOT / "config" / "strategies" / "kronos.yaml"
GOLDEN_PATH = ROOT / "tests" / "golden" / "kronos" / "cross_market_evaluation_v1.json"
NOW = datetime(2026, 7, 13, 2, tzinfo=UTC)
START = datetime(2023, 1, 2, 14, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
MARKETS = (
    ("US", "XNAS", UUID("10000000-0000-4000-8000-000000000801")),
    ("HK", "XHKG", UUID("10000000-0000-4000-8000-000000000802")),
    ("TW", "XTAI", UUID("10000000-0000-4000-8000-000000000803")),
)


def _forecast(index: int) -> ForecastOutputArtifact:
    market, _, instrument_id = MARKETS[index % len(MARKETS)]
    generated_at = START + timedelta(hours=index)
    forecast = ForecastSignal(
        forecast_id=uuid5(NAMESPACE_URL, f"kronos-evaluation:{index}"),
        instrument_id=instrument_id,
        as_of=generated_at - timedelta(minutes=1),
        interval="1d",
        horizon_bars=1,
        expected_return=Decimal(0),
        median_return=Decimal(0),
        direction_probability=Decimal("0.5"),
        expected_volatility=Decimal("0.01"),
        downside_quantile=Decimal("-0.02"),
        max_drawdown_quantile=Decimal("-0.02"),
        path_count=16,
        dispersion=Decimal("0.01"),
        input_quality=DataQuality(
            status=DataQualityStatus.AVAILABLE,
            completeness=Decimal(1),
        ),
        model_id="NeoQuasar/Kronos-small",
        model_revision="901c26c1332695a2a8f243eb2f37243a37bea320",
        tokenizer_id="NeoQuasar/Kronos-Tokenizer-base",
        tokenizer_revision="0e0117387f39004a9016484a186a908917e22426",
        device="cpu",
        seed_policy="explicit-sequential-v1",
        inference_code_version="kronos-path-retention/1.0.0",
        dataset_snapshot_id=uuid5(NAMESPACE_URL, f"kronos-source:{market}:{index}"),
        input_window_start=generated_at - timedelta(days=2),
        input_window_end=generated_at - timedelta(minutes=2),
        generated_at=generated_at,
        latency_ms=10,
    )
    return ForecastOutputArtifact(
        request_id=uuid5(NAMESPACE_URL, f"kronos-request:{index}"),
        forecast=forecast,
        raw_output_artifact_ref=f"sha256:{HASH_A}",
        sampled_paths_artifact_ref=f"sha256:{HASH_B}",
        model_artifact_hash=(
            "b082dfcbd8e8c142a725c8bbb99781802f38fec81210e13479effb32b3c3e020"
        ),
        tokenizer_artifact_hash=(
            "59d85f6af76a2c3b8240ea06cb21db4213b4eeca053f246b23e29cf832fc6bee"
        ),
        runtime_hash=(
            "0e1ac0d0a47253106d082fb30b47d3159ac67ec4363f2753efc9e1d98d76f328"
        ),
        data_hash=HASH_A,
        stochastic=True,
        created_at=generated_at,
    )


def _record(index: int) -> KronosEvaluationRecord:
    market, mic, instrument_id = MARKETS[index % len(MARKETS)]
    output = _forecast(index)
    prediction_at = output.created_at
    actual_return = Decimal("0.01") if index % 2 else Decimal("-0.01")
    return KronosEvaluationRecord(
        observation_id=uuid5(NAMESPACE_URL, f"kronos-observation:{index}"),
        market=market,
        mic=mic,
        instrument_id=instrument_id,
        forecast_output=output,
        feature_event_at=output.forecast.input_window_end,
        feature_available_at=output.forecast.as_of,
        outcome_at=prediction_at + timedelta(minutes=30),
        label_available_at=prediction_at + timedelta(minutes=31),
        universe_known_at=prediction_at - timedelta(days=30),
        availability_certainty="proven",
        in_historical_universe=True,
        label_start_close=Decimal(100),
        label_end_close=Decimal(100) * (Decimal(1) + actual_return),
        benchmark_start_close=Decimal(100),
        benchmark_end_close=Decimal("100.01"),
        turnover=Decimal("0.25"),
        baselines=(
            KronosBaselinePrediction(
                candidate_id="baseline-last-value/1.0.0",
                predicted_return=Decimal(0),
            ),
            KronosBaselinePrediction(
                candidate_id="baseline-moving-average/1.0.0",
                predicted_return=Decimal(0),
            ),
            KronosBaselinePrediction(
                candidate_id="baseline-linear/1.0.0",
                predicted_return=Decimal(0),
            ),
        ),
    )


def _snapshot(count: int = 768) -> KronosEvaluationSnapshot:
    return KronosEvaluationSnapshot(
        snapshot_id=UUID("10000000-0000-4000-8000-000000000804"),
        as_of=NOW,
        universe_artifact_ref=f"sha256:{HASH_A}",
        records=tuple(_record(index) for index in range(count)),
    )


def _request(snapshot: KronosEvaluationSnapshot) -> EvaluationRequest:
    configuration = load_kronos_strategy_configuration(STRATEGY_PATH)
    policy = load_evaluation_policy(POLICY_PATH)
    return EvaluationRequest(
        request_id=UUID("10000000-0000-4000-8000-000000000805"),
        manifest=configuration.manifest,
        dataset_snapshot_id=snapshot.snapshot_id,
        snapshot_artifact_ref=f"sha256:{snapshot.data_hash}",
        data_hash=snapshot.data_hash,
        as_of=snapshot.as_of,
        window_start=snapshot.records[0].forecast_output.created_at,
        window_end=snapshot.records[-1].forecast_output.created_at,
        evaluation_policy_hash=policy.policy_hash,
        requested_at=NOW,
        deadline_at=NOW + timedelta(minutes=10),
    )


def test_committed_kronos_strategy_is_exact_shadow_and_policy_bound() -> None:
    configuration = load_kronos_strategy_configuration(STRATEGY_PATH)
    policy = load_evaluation_policy(POLICY_PATH)
    worker = load_kronos_worker_configuration(
        ROOT / "config" / "workers" / "kronos_cpu.yaml"
    )

    assert configuration.deployment_state == "shadow"
    assert configuration.paper_weight == Decimal(0)
    assert configuration.evaluation_policy_hash == policy.policy_hash
    assert configuration.manifest.split_policy_hash == policy.policy_hash
    assert configuration.required_markets == ("US", "HK", "TW")
    assert configuration.manifest.runtime_hash == worker.runtime.runtime_hash
    assert configuration.model_id == worker.runtime.model_id
    assert configuration.model_revision == worker.runtime.model_revision
    assert configuration.model_artifact_hash == worker.runtime.model_artifact_hash
    assert configuration.tokenizer_id == worker.runtime.tokenizer_id
    assert configuration.tokenizer_revision == worker.runtime.tokenizer_revision
    assert (
        configuration.tokenizer_artifact_hash == worker.runtime.tokenizer_artifact_hash
    )
    assert configuration.cost_spec.fee_bps == policy.fee_bps
    assert configuration.cost_spec.slippage_bps == policy.slippage_bps
    assert configuration.cost_spec.cost_multipliers == policy.cost_multipliers


def test_cross_market_archived_forecasts_build_hash_bound_pit_inputs() -> None:
    snapshot = _snapshot()

    dataset, baselines = build_kronos_evaluation_inputs(snapshot)

    assert dataset.data_hash == snapshot.data_hash
    assert len(dataset.observations) == 768
    assert {record.market for record in snapshot.records} == {"US", "HK", "TW"}
    assert tuple(value.candidate_id for value in baselines) == (
        "baseline-last-value/1.0.0",
        "baseline-moving-average/1.0.0",
        "baseline-linear/1.0.0",
    )
    assert dataset.observations[0].predicted_return == Decimal(0)


def test_production_thresholds_generate_expected_shadow_only_golden_report() -> None:
    snapshot = _snapshot()
    policy = load_evaluation_policy(POLICY_PATH)

    result = evaluate_kronos_snapshot(
        request=_request(snapshot),
        snapshot=snapshot,
        policy=policy,
        report_id=UUID("10000000-0000-4000-8000-000000000806"),
        report_artifact_ref=f"sha256:{HASH_A}",
        created_at=NOW,
    )

    assert isinstance(result, Success)
    report = result.value
    actual = {
        "passed": report.passed,
        "calibration": report.calibration.value,
        "failed_checks": sorted(
            check.kind.value
            for check in report.checks
            if check.status is EvaluationCheckStatus.FAILED
        ),
        "metrics": {
            f"{metric.name}:{metric.segment}": str(metric.value)
            for metric in report.metrics
            if metric.name
            in {
                "brier_score",
                "calibration_error",
                "out_of_sample_observations",
                "walk_forward_splits",
            }
        },
        "markets": sorted({record.market for record in snapshot.records}),
    }
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert actual == expected


def test_request_or_future_label_contamination_fails_closed() -> None:
    snapshot = _snapshot()
    mismatched = _request(snapshot).model_copy(update={"data_hash": "f" * 64})
    mismatch = evaluate_kronos_snapshot(
        request=mismatched,
        snapshot=snapshot,
        policy=load_evaluation_policy(POLICY_PATH),
        report_id=UUID("10000000-0000-4000-8000-000000000807"),
        report_artifact_ref=f"sha256:{HASH_A}",
        created_at=NOW,
    )
    records = list(snapshot.records)
    records[0] = records[0].model_copy(
        update={"label_available_at": NOW + timedelta(seconds=1)}
    )
    contaminated = snapshot.model_copy(update={"records": tuple(records)})
    leakage = evaluate_kronos_snapshot(
        request=_request(contaminated),
        snapshot=contaminated,
        policy=load_evaluation_policy(POLICY_PATH),
        report_id=UUID("10000000-0000-4000-8000-000000000808"),
        report_artifact_ref=f"sha256:{HASH_A}",
        created_at=NOW,
    )

    assert isinstance(mismatch, Failure)
    assert isinstance(leakage, Failure)


def test_snapshot_rejects_mixed_runtime_or_baseline_identity() -> None:
    first = _record(0)
    second = _record(1)
    changed_output = second.forecast_output.model_copy(update={"runtime_hash": HASH_A})
    with pytest.raises(ValidationError, match="runtime"):
        KronosEvaluationSnapshot(
            snapshot_id=UUID("10000000-0000-4000-8000-000000000809"),
            as_of=NOW,
            universe_artifact_ref=f"sha256:{HASH_A}",
            records=(
                first,
                second.model_copy(update={"forecast_output": changed_output}),
            ),
        )

    changed_baselines = second.baselines[:-1]
    with pytest.raises(ValidationError, match="baseline"):
        KronosEvaluationSnapshot(
            snapshot_id=UUID("10000000-0000-4000-8000-000000000810"),
            as_of=NOW,
            universe_artifact_ref=f"sha256:{HASH_A}",
            records=(first, second.model_copy(update={"baselines": changed_baselines})),
        )


def test_model_copy_cannot_bypass_snapshot_runtime_fence() -> None:
    snapshot = _snapshot()
    records = list(snapshot.records)
    changed = records[1].forecast_output.model_copy(update={"runtime_hash": HASH_A})
    records[1] = records[1].model_copy(update={"forecast_output": changed})
    bypassed = snapshot.model_copy(update={"records": tuple(records)})

    result = evaluate_kronos_snapshot(
        request=_request(bypassed),
        snapshot=bypassed,
        policy=load_evaluation_policy(POLICY_PATH),
        report_id=UUID("10000000-0000-4000-8000-000000000811"),
        report_artifact_ref=f"sha256:{HASH_A}",
        created_at=NOW,
    )

    assert isinstance(result, Failure)
