from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from stonks_agent.application.evaluation.contracts import (
    CandidatePredictionSeries,
    EvaluationDataset,
    EvaluationObservation,
    EvaluationPolicy,
)
from stonks_agent.domain.evaluation import EvaluationRequest
from stonks_agent.domain.strategy import StrategyKind, StrategyManifest

NOW = datetime(2026, 7, 13, 2, tzinfo=UTC)
SNAPSHOT_ID = UUID("00000000-0000-4000-8000-000000000501")
MANIFEST_ID = UUID("00000000-0000-4000-8000-000000000502")
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64


def manifest() -> StrategyManifest:
    return StrategyManifest(
        manifest_id=MANIFEST_ID,
        strategy_id="candidate-alpha",
        strategy_version="1.0.0",
        kind=StrategyKind.DETERMINISTIC,
        source_artifact_ref=f"sha256:{HASH_A}",
        runtime_hash=HASH_B,
        feature_spec_hash=HASH_C,
        label_spec_hash=HASH_D,
        universe_spec_hash=HASH_E,
        cost_model_hash=HASH_A,
        split_policy_hash=HASH_B,
        parameters_hash=HASH_C,
        owner="quant-research",
        deterministic=True,
        created_at=NOW,
    )


def request() -> EvaluationRequest:
    return EvaluationRequest(
        request_id=UUID("00000000-0000-4000-8000-000000000503"),
        manifest=manifest(),
        dataset_snapshot_id=SNAPSHOT_ID,
        snapshot_artifact_ref=f"sha256:{HASH_D}",
        data_hash=HASH_C,
        as_of=NOW,
        window_start=NOW - timedelta(days=60),
        window_end=NOW - timedelta(days=1),
        evaluation_policy_hash=policy().policy_hash,
        requested_at=NOW,
        deadline_at=NOW + timedelta(minutes=10),
    )


def observation(
    index: int,
    *,
    predicted: str | None = None,
    actual: str | None = None,
    probability: str | None = None,
    **changes: object,
) -> EvaluationObservation:
    prediction_at = NOW - timedelta(days=40 - index)
    actual_value = actual or ("0.02" if index % 3 != 0 else "-0.01")
    predicted_value = predicted or ("0.03" if actual_value[0] != "-" else "-0.02")
    payload: dict[str, object] = {
        "observation_id": uuid5(NAMESPACE_URL, f"evaluation:{index}"),
        "instrument_id": UUID("00000000-0000-4000-8000-000000000504"),
        "event_at": prediction_at - timedelta(days=2),
        "feature_available_at": prediction_at - timedelta(days=1),
        "prediction_at": prediction_at,
        "outcome_at": prediction_at + timedelta(days=1),
        "label_available_at": prediction_at + timedelta(days=1, minutes=1),
        "universe_known_at": prediction_at - timedelta(days=30),
        "availability_certainty": "proven",
        "in_historical_universe": True,
        "predicted_return": Decimal(predicted_value),
        "actual_return": Decimal(actual_value),
        "benchmark_return": Decimal("0.001"),
        "direction_probability": Decimal(
            probability or ("0.8" if predicted_value[0] != "-" else "0.2")
        ),
        "turnover": Decimal("0.25"),
    }
    return EvaluationObservation.model_validate(payload | changes)


def dataset(count: int = 24, **changes: object) -> EvaluationDataset:
    payload: dict[str, object] = {
        "dataset_snapshot_id": SNAPSHOT_ID,
        "data_hash": HASH_C,
        "as_of": NOW,
        "universe_artifact_ref": f"sha256:{HASH_A}",
        "observations": tuple(observation(index) for index in range(count)),
    }
    return EvaluationDataset.model_validate(payload | changes)


def baselines(count: int = 24) -> tuple[CandidatePredictionSeries, ...]:
    return (
        CandidatePredictionSeries(
            candidate_id="baseline-last-value/1.0.0",
            predictions=tuple(Decimal("0") for _ in range(count)),
        ),
        CandidatePredictionSeries(
            candidate_id="baseline-moving-average/1.0.0",
            predictions=tuple(
                Decimal("0.005") if index % 2 else Decimal("-0.005")
                for index in range(count)
            ),
        ),
        CandidatePredictionSeries(
            candidate_id="baseline-linear/1.0.0",
            predictions=tuple(Decimal("0.01") for _ in range(count)),
        ),
    )


def policy() -> EvaluationPolicy:
    return EvaluationPolicy(
        minimum_observations=12,
        train_size=8,
        test_size=4,
        step_size=4,
        purge_observations=1,
        embargo_observations=1,
        cpcv_groups=4,
        max_pbo=Decimal("0.75"),
        fee_bps=Decimal("1"),
        slippage_bps=Decimal("2"),
        cost_multipliers=(Decimal("0.5"), Decimal("1"), Decimal("2")),
        minimum_net_alpha=Decimal("0"),
        maximum_drawdown=Decimal("0.25"),
        maximum_brier_score=Decimal("0.25"),
        maximum_calibration_error=Decimal("0.25"),
        calibration_buckets=5,
        report_valid_days=30,
    )
