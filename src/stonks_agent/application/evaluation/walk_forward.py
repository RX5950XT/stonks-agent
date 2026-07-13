"""Purged walk-forward splits and bounded combinatorial PBO estimation."""

from __future__ import annotations

from decimal import Decimal
from itertools import combinations

from stonks_agent.application.evaluation.contracts import (
    CandidatePredictionSeries,
    EvaluationDataset,
    EvaluationPolicy,
    WalkForwardSplit,
    mean,
    position,
    quantize,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)


def build_purged_walk_forward_splits(
    dataset: EvaluationDataset,
    policy: EvaluationPolicy,
) -> Result[tuple[WalkForwardSplit, ...]]:
    gap = policy.purge_observations + policy.embargo_observations
    test_start = policy.train_size + gap
    splits: list[WalkForwardSplit] = []
    while test_start + policy.test_size <= len(dataset.observations):
        train_end = test_start - gap
        train_start = train_end - policy.train_size
        train = dataset.observations[train_start:train_end]
        test = dataset.observations[test_start : test_start + policy.test_size]
        splits.append(
            WalkForwardSplit(
                split_id=len(splits) + 1,
                train_observation_ids=tuple(value.observation_id for value in train),
                test_observation_ids=tuple(value.observation_id for value in test),
                purge_observations=policy.purge_observations,
                embargo_observations=policy.embargo_observations,
            )
        )
        test_start += policy.step_size
    if len(splits) < policy.minimum_splits:
        return Failure(
            StructuredError(
                code=ErrorCode.INVALID_INPUT,
                message="Evaluation dataset cannot satisfy walk-forward policy",
            )
        )
    return Success(tuple(splits))


def estimate_probability_of_backtest_overfitting(
    dataset: EvaluationDataset,
    candidates: tuple[CandidatePredictionSeries, ...],
    *,
    groups: int,
) -> Decimal | None:
    if len(candidates) < 2 or len(dataset.observations) < groups * 2:
        return None
    if not 4 <= groups <= 8 or groups % 2:
        raise ValueError("CPCV groups must be an even value between four and eight")
    if any(len(value.predictions) != len(dataset.observations) for value in candidates):
        raise ValueError("CPCV candidate predictions must align with observations")
    group_indices = _groups(len(dataset.observations), groups)
    below_median = 0
    trials = 0
    for train_groups in combinations(range(groups), groups // 2):
        train_indices = tuple(
            index for group in train_groups for index in group_indices[group]
        )
        test_indices = tuple(
            index
            for group in range(groups)
            if group not in train_groups
            for index in group_indices[group]
        )
        selected = max(
            candidates,
            key=lambda value: (
                _candidate_mean(value, dataset, train_indices),
                value.candidate_id,
            ),
        )
        ranked = sorted(
            candidates,
            key=lambda value: (
                _candidate_mean(value, dataset, test_indices),
                value.candidate_id,
            ),
        )
        if ranked.index(selected) < len(ranked) / 2:
            below_median += 1
        trials += 1
    return quantize(Decimal(below_median) / Decimal(trials))


def _groups(count: int, groups: int) -> tuple[tuple[int, ...], ...]:
    return tuple(
        tuple(range(group * count // groups, (group + 1) * count // groups))
        for group in range(groups)
    )


def _candidate_mean(
    candidate: CandidatePredictionSeries,
    dataset: EvaluationDataset,
    indices: tuple[int, ...],
) -> Decimal:
    returns = tuple(
        position(candidate.predictions[index])
        * dataset.observations[index].actual_return
        for index in indices
    )
    return mean(returns)
