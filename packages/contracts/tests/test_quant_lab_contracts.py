from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from pydantic import ValidationError

from stonks_contracts.common import stable_payload_hash
from stonks_contracts.quant_lab import (
    QuantBacktestPosition,
    QuantCostModelSpec,
    QuantDatasetArtifact,
    QuantDatasetRow,
    QuantFeatureName,
    QuantFeatureSpec,
    QuantLabelSpec,
    QuantMetric,
    QuantModelSpec,
    QuantPrediction,
    QuantResearchJob,
    QuantResearchResult,
    QuantRuntimeIdentity,
    QuantSplitSpec,
    QuantUniverseSpec,
    QuantWorkerResponse,
)

NOW = datetime(2026, 7, 13, 4, tzinfo=UTC)
START = datetime(2024, 1, 2, 21, tzinfo=UTC)
INSTRUMENTS = (
    UUID("20000000-0000-4000-8000-000000000901"),
    UUID("20000000-0000-4000-8000-000000000902"),
)
HASH_A = "a" * 64
HASH_B = "b" * 64


def feature_spec() -> QuantFeatureSpec:
    return QuantFeatureSpec(
        names=(
            QuantFeatureName.RETURN_1,
            QuantFeatureName.RETURN_5,
            QuantFeatureName.VOLATILITY_5,
            QuantFeatureName.VOLUME_CHANGE_1,
        ),
        lookback_bars=6,
    )


def label_spec() -> QuantLabelSpec:
    return QuantLabelSpec(name="forward_return", horizon_bars=1)


def universe_spec() -> QuantUniverseSpec:
    return QuantUniverseSpec(
        instrument_ids=INSTRUMENTS,
        historical_membership_artifact_ref=f"sha256:{HASH_A}",
    )


def cost_spec() -> QuantCostModelSpec:
    return QuantCostModelSpec(fee_bps=Decimal(1), slippage_bps=Decimal(5))


def split_spec() -> QuantSplitSpec:
    return QuantSplitSpec(
        train_start=START,
        train_end=START + timedelta(days=4),
        valid_start=START + timedelta(days=5),
        valid_end=START + timedelta(days=6),
        test_start=START + timedelta(days=7),
        test_end=START + timedelta(days=8),
        purge_observations=1,
        embargo_observations=1,
    )


def runtime() -> QuantRuntimeIdentity:
    return QuantRuntimeIdentity(
        worker_version="quant-lab-worker/0.1.0",
        qlib_commit="d5379c520f66a39953bad76234a7019a72796fd0",
        qlib_source_hash=HASH_A,
        qlib_version="0.9.8.dev0+d5379c52",
        runtime_hash=HASH_B,
        python_version="3.12.9",
        numpy_version="2.2.6",
        pandas_version="2.2.3",
        sklearn_version="1.7.2",
    )


def row(index: int, **changes: object) -> QuantDatasetRow:
    event_at = START + timedelta(days=index // 2, seconds=index % 2)
    payload: dict[str, object] = {
        "row_id": uuid5(NAMESPACE_URL, f"quant-row:{index}"),
        "instrument_id": INSTRUMENTS[index % 2],
        "event_at": event_at,
        "feature_available_at": event_at,
        "label_outcome_at": event_at + timedelta(hours=1),
        "label_available_at": event_at + timedelta(hours=1, minutes=1),
        "historical_universe_known_at": event_at - timedelta(days=30),
        "in_historical_universe": True,
        "features": ("0.01", "0.02", "0.03", "0.04"),
        "label": "0.01" if index % 2 else "-0.01",
    }
    return QuantDatasetRow.model_validate(payload | changes)


def dataset(**changes: object) -> QuantDatasetArtifact:
    payload: dict[str, object] = {
        "dataset_snapshot_id": UUID("20000000-0000-4000-8000-000000000903"),
        "source_snapshot_artifact_ref": f"sha256:{HASH_A}",
        "source_data_hash": HASH_B,
        "as_of": NOW,
        "feature_spec": feature_spec(),
        "label_spec": label_spec(),
        "universe_spec": universe_spec(),
        "rows": tuple(row(index) for index in range(18)),
    }
    return QuantDatasetArtifact.model_validate(payload | changes)


def job(**changes: object) -> QuantResearchJob:
    artifact = dataset()
    payload: dict[str, object] = {
        "request_id": UUID("20000000-0000-4000-8000-000000000904"),
        "run_id": UUID("20000000-0000-4000-8000-000000000905"),
        "job_id": UUID("20000000-0000-4000-8000-000000000906"),
        "attempt_generation": 2,
        "attempt_nonce": "nonce-2",
        "dataset_artifact_ref": f"sha256:{artifact.payload_hash()}",
        "dataset": artifact,
        "feature_spec": feature_spec(),
        "label_spec": label_spec(),
        "universe_spec": universe_spec(),
        "cost_model": cost_spec(),
        "split_policy": split_spec(),
        "model_spec": QuantModelSpec(
            algorithm="qlib_linear_ols",
            fit_intercept=False,
            deterministic=True,
        ),
        "runtime": runtime(),
        "requested_at": NOW,
        "deadline": NOW + timedelta(minutes=5),
    }
    return QuantResearchJob.model_validate(payload | changes)


def result() -> QuantResearchResult:
    predictions = tuple(
        QuantPrediction(
            row_id=value.row_id,
            instrument_id=value.instrument_id,
            event_at=value.event_at,
            predicted_return=Decimal("0.005") if index % 2 else Decimal("-0.005"),
            actual_return=value.label,
        )
        for index, value in enumerate(dataset().rows[-4:])
    )
    positions = tuple(
        QuantBacktestPosition(
            row_id=value.row_id,
            instrument_id=value.instrument_id,
            event_at=value.event_at,
            research_exposure=Decimal(1) if value.predicted_return > 0 else Decimal(-1),
        )
        for value in predictions
    )
    metrics = (
        QuantMetric(name="mean_squared_error", value=Decimal("0.000025"), unit="ratio"),
        QuantMetric(name="mean_net_return", value=Decimal("0.0094"), unit="return"),
    )
    parameters = (Decimal("0.1"), Decimal("0.2"), Decimal("0.3"), Decimal("0.4"))
    return QuantResearchResult(
        request_id=job().request_id,
        dataset_snapshot_id=dataset().dataset_snapshot_id,
        source_data_hash=dataset().source_data_hash,
        dataset_artifact_hash=dataset().payload_hash(),
        feature_spec_hash=feature_spec().spec_hash,
        label_spec_hash=label_spec().spec_hash,
        universe_spec_hash=universe_spec().spec_hash,
        cost_model_hash=cost_spec().spec_hash,
        split_policy_hash=split_spec().spec_hash,
        model_spec_hash=job().model_spec.spec_hash,
        runtime=runtime(),
        predictions=predictions,
        positions=positions,
        metrics=metrics,
        model_parameters=parameters,
        prediction_artifact_hash=stable_payload_hash(
            [value.model_dump(mode="json") for value in predictions]
        ),
        position_artifact_hash=stable_payload_hash(
            [value.model_dump(mode="json") for value in positions]
        ),
        metrics_artifact_hash=stable_payload_hash(
            [value.model_dump(mode="json") for value in metrics]
        ),
        model_artifact_hash=stable_payload_hash([str(value) for value in parameters]),
        deterministic=True,
        generated_at=NOW + timedelta(minutes=1),
        warnings=(),
    )


def test_quant_job_is_frozen_artifact_and_spec_bound() -> None:
    value = job()

    assert value.dataset_artifact_ref == f"sha256:{value.dataset.payload_hash()}"
    assert value.feature_spec.spec_hash == value.dataset.feature_spec.spec_hash
    assert value.model_spec.deterministic is True
    assert value.attempt_nonce == "nonce-2"


@pytest.mark.parametrize(
    "change",
    [
        {"dataset_artifact_ref": f"sha256:{HASH_A}"},
        {"feature_spec": QuantFeatureSpec(names=(QuantFeatureName.RETURN_1,), lookback_bars=2)},
        {"deadline": NOW},
    ],
)
def test_job_rejects_artifact_spec_or_deadline_drift(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        job(**change)


def test_dataset_rejects_future_unknown_or_misaligned_rows() -> None:
    with pytest.raises(ValidationError, match="feature"):
        dataset(rows=(row(0, features=("0.1",)), row(1)))
    with pytest.raises(ValidationError, match="point-in-time"):
        dataset(
            rows=(
                row(0, label_available_at=NOW + timedelta(seconds=1)),
                row(1),
            )
        )
    with pytest.raises(ValidationError, match="universe"):
        dataset(rows=(row(0, in_historical_universe=False), row(1)))


def test_result_hashes_and_response_fence_are_verified() -> None:
    value = result()
    response = QuantWorkerResponse(
        request_id=job().request_id,
        run_id=job().run_id,
        job_id=job().job_id,
        attempt_generation=job().attempt_generation,
        attempt_nonce=job().attempt_nonce,
        result_artifact_hash=value.payload_hash(),
        result=value,
    )

    assert response.result_artifact_hash == response.result.payload_hash()
    with pytest.raises(ValidationError, match="result artifact hash"):
        response.model_copy(update={"result_artifact_hash": HASH_A}).model_validate(
            response.model_dump() | {"result_artifact_hash": HASH_A}
        )


def test_result_rejects_tampered_artifact_hash_or_position_alignment() -> None:
    value = result()
    with pytest.raises(ValidationError, match="artifact hash"):
        QuantResearchResult.model_validate(
            value.model_dump() | {"prediction_artifact_hash": HASH_A}
        )
    with pytest.raises(ValidationError, match="position"):
        QuantResearchResult.model_validate(value.model_dump() | {"positions": ()})


def test_quant_contracts_reject_execution_shaped_fields() -> None:
    forbidden = ("target_weight", "quantity", "order_intent", "risk_override")
    for field in forbidden:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            QuantResearchJob.model_validate(job().model_dump() | {field: "1"})
        with pytest.raises(ValidationError, match="extra_forbidden"):
            QuantResearchResult.model_validate(result().model_dump() | {field: "1"})
