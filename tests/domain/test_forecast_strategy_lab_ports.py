from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from stonks_agent.domain.evaluation import EvaluationRequest
from stonks_agent.domain.signal import ForecastOutputArtifact, ForecastRequest
from stonks_agent.domain.strategy import StrategyKind, StrategyManifest
from stonks_agent.ports.forecast import ForecastPort
from stonks_agent.ports.strategy_lab import StrategyLabPort
from stonks_contracts.market_data import DataQuality, DataQualityStatus
from stonks_contracts.signal import ForecastSignal

NOW = datetime(2026, 7, 13, 2, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
INSTRUMENT_ID = UUID("00000000-0000-4000-8000-000000000201")
SNAPSHOT_ID = UUID("00000000-0000-4000-8000-000000000202")
REQUEST_ID = UUID("00000000-0000-4000-8000-000000000203")


def forecast_request() -> ForecastRequest:
    return ForecastRequest(
        request_id=REQUEST_ID,
        run_id=UUID("00000000-0000-4000-8000-000000000204"),
        instrument_id=INSTRUMENT_ID,
        dataset_snapshot_id=SNAPSHOT_ID,
        snapshot_artifact_ref=f"sha256:{HASH_A}",
        data_hash=HASH_B,
        as_of=NOW,
        interval="1d",
        horizon_bars=5,
        input_window_start=NOW - timedelta(days=30),
        input_window_end=NOW,
        model_id="kronos",
        model_revision=HASH_C,
        runtime_hash=HASH_A,
        requested_at=NOW,
        deadline_at=NOW + timedelta(minutes=5),
    )


def forecast_signal() -> ForecastSignal:
    return ForecastSignal(
        forecast_id=UUID("00000000-0000-4000-8000-000000000205"),
        instrument_id=INSTRUMENT_ID,
        as_of=NOW,
        interval="1d",
        horizon_bars=5,
        expected_return=Decimal("0.02"),
        median_return=Decimal("0.01"),
        direction_probability=Decimal("0.6"),
        expected_volatility=Decimal("0.2"),
        downside_quantile=Decimal("-0.1"),
        max_drawdown_quantile=Decimal("-0.2"),
        path_count=10,
        dispersion=Decimal("0.1"),
        input_quality=DataQuality(
            status=DataQualityStatus.AVAILABLE,
            completeness=Decimal("1"),
        ),
        model_id="kronos",
        model_revision=HASH_C,
        tokenizer_id="kronos-tokenizer",
        tokenizer_revision=HASH_C,
        device="cpu",
        seed_policy="archived-paths",
        inference_code_version="1.0.0",
        dataset_snapshot_id=SNAPSHOT_ID,
        input_window_start=NOW - timedelta(days=30),
        input_window_end=NOW,
        generated_at=NOW + timedelta(seconds=1),
        latency_ms=1,
    )


def test_forecast_output_requires_exact_request_binding_and_archived_paths() -> None:
    request = forecast_request()
    output = ForecastOutputArtifact(
        request_id=request.request_id,
        forecast=forecast_signal(),
        raw_output_artifact_ref=f"sha256:{HASH_B}",
        sampled_paths_artifact_ref=f"sha256:{HASH_C}",
        model_artifact_hash=HASH_C,
        runtime_hash=request.runtime_hash,
        data_hash=request.data_hash,
        stochastic=True,
        created_at=NOW + timedelta(seconds=1),
    )

    assert output.sampled_paths_artifact_ref == f"sha256:{HASH_C}"
    with pytest.raises(ValidationError, match="forecast output does not match request"):
        ForecastOutputArtifact.model_validate(
            output.model_dump() | {"request_id": UUID(int=999)},
            context={"request": request},
        )


def test_forecast_request_rejects_future_input_and_order_authority() -> None:
    payload = forecast_request().model_dump()
    with pytest.raises(ValidationError, match="input window"):
        ForecastRequest.model_validate(
            payload | {"input_window_end": NOW + timedelta(seconds=1)}
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ForecastRequest.model_validate(payload | {"target_weight": "0.5"})


def test_evaluation_request_is_artifact_only_and_pit_bounded() -> None:
    value = EvaluationRequest(
        request_id=UUID("00000000-0000-4000-8000-000000000206"),
        manifest=StrategyManifest(
            manifest_id=UUID("00000000-0000-4000-8000-000000000207"),
            strategy_id="baseline-linear",
            strategy_version="1.0.0",
            kind=StrategyKind.DETERMINISTIC,
            source_artifact_ref=f"sha256:{HASH_A}",
            runtime_hash=HASH_A,
            feature_spec_hash=HASH_A,
            label_spec_hash=HASH_A,
            universe_spec_hash=HASH_A,
            cost_model_hash=HASH_A,
            split_policy_hash=HASH_A,
            parameters_hash=HASH_A,
            owner="quant-research",
            deterministic=True,
            created_at=NOW,
        ),
        dataset_snapshot_id=SNAPSHOT_ID,
        snapshot_artifact_ref=f"sha256:{HASH_B}",
        data_hash=HASH_B,
        as_of=NOW,
        window_start=NOW - timedelta(days=365),
        window_end=NOW - timedelta(days=1),
        evaluation_policy_hash=HASH_C,
        requested_at=NOW,
        deadline_at=NOW + timedelta(minutes=30),
    )

    assert value.manifest.runtime_hash == value.runtime_hash
    with pytest.raises(ValidationError, match="extra_forbidden"):
        EvaluationRequest.model_validate(value.model_dump() | {"promote": True})


def test_forecast_and_strategy_lab_are_runtime_checkable_typed_ports() -> None:
    class ForecastAdapter:
        def forecast(self, request: object) -> object:
            return request

    class StrategyLabAdapter:
        def evaluate(self, request: object) -> object:
            return request

    assert isinstance(ForecastAdapter(), ForecastPort)
    assert isinstance(StrategyLabAdapter(), StrategyLabPort)
