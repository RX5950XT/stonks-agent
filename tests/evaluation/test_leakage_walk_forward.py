from __future__ import annotations

from datetime import timedelta

import pytest

from stonks_agent.application.evaluation.leakage import audit_dataset
from stonks_agent.application.evaluation.walk_forward import (
    build_purged_walk_forward_splits,
    estimate_probability_of_backtest_overfitting,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Success

from .helpers import NOW, baselines, dataset, observation, policy


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"feature_available_at": NOW}, "feature_not_available_at_prediction"),
        (
            {"feature_available_at": NOW - timedelta(days=43)},
            "feature_availability_precedes_event",
        ),
        ({"label_available_at": NOW - timedelta(days=40)}, "label_leakage"),
        ({"availability_certainty": "unknown"}, "publication_lag_unknown"),
        ({"universe_known_at": NOW}, "historical_universe_unknown"),
        ({"in_historical_universe": False}, "survivorship_contamination"),
        (
            {"label_available_at": NOW + timedelta(seconds=1)},
            "outcome_unavailable_at_as_of",
        ),
        (
            {"label_available_at": NOW - timedelta(days=39, hours=12)},
            "label_availability_precedes_outcome",
        ),
    ],
)
def test_contaminated_point_in_time_dataset_fails_closed(
    changes: dict[str, object],
    reason: str,
) -> None:
    observations = list(dataset().observations)
    observations[0] = observation(0, **changes)

    result = audit_dataset(dataset(observations=tuple(observations)))

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.INVALID_INPUT
    assert result.error.details["reason_code"] == reason


def test_clean_dataset_passes_all_three_authority_audits() -> None:
    result = audit_dataset(dataset())

    assert isinstance(result, Success)
    assert result.value.point_in_time_passed is True
    assert result.value.leakage_passed is True
    assert result.value.survivorship_passed is True


def test_walk_forward_splits_have_explicit_purge_and_embargo_gap() -> None:
    value = dataset()
    result = build_purged_walk_forward_splits(value, policy())

    assert isinstance(result, Success)
    assert len(result.value) == 3
    for split in result.value:
        assert set(split.train_observation_ids).isdisjoint(split.test_observation_ids)
        train_positions = [
            index
            for index, item in enumerate(value.observations)
            if item.observation_id in split.train_observation_ids
        ]
        test_positions = [
            index
            for index, item in enumerate(value.observations)
            if item.observation_id in split.test_observation_ids
        ]
        assert min(test_positions) - max(train_positions) == 3


def test_cpcv_pbo_is_bounded_and_deterministic_when_multiple_candidates_exist() -> None:
    value = dataset()
    candidate_predictions = (
        *baselines(),
        # Primary strategy is deliberately strongest for this clean fixture.
        type(baselines()[0])(
            candidate_id="candidate-alpha/1.0.0",
            predictions=tuple(item.predicted_return for item in value.observations),
        ),
    )

    first = estimate_probability_of_backtest_overfitting(
        value, candidate_predictions, groups=4
    )
    second = estimate_probability_of_backtest_overfitting(
        value, candidate_predictions, groups=4
    )

    assert first == second
    assert first is not None
    assert 0 <= first <= 1
