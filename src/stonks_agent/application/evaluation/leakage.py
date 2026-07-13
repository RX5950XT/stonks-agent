"""Point-in-time, publication-lag, leakage, and survivorship audits."""

from __future__ import annotations

from stonks_agent.application.evaluation.contracts import (
    EvaluationAuditSummary,
    EvaluationDataset,
    EvaluationObservation,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)


def audit_dataset(dataset: EvaluationDataset) -> Result[EvaluationAuditSummary]:
    for observation in dataset.observations:
        reason = _contamination_reason(observation, dataset)
        if reason is not None:
            return Failure(
                StructuredError(
                    code=ErrorCode.INVALID_INPUT,
                    message="Evaluation dataset failed point-in-time audit",
                    details={"reason_code": reason},
                )
            )
    return Success(EvaluationAuditSummary())


def _contamination_reason(
    observation: EvaluationObservation,
    dataset: EvaluationDataset,
) -> str | None:
    if observation.availability_certainty != "proven":
        return "publication_lag_unknown"
    if (
        observation.event_at > observation.prediction_at
        or observation.feature_available_at > observation.prediction_at
    ):
        return "feature_not_available_at_prediction"
    if observation.feature_available_at < observation.event_at:
        return "feature_availability_precedes_event"
    if (
        observation.outcome_at <= observation.prediction_at
        or observation.label_available_at <= observation.prediction_at
    ):
        return "label_leakage"
    if observation.label_available_at > dataset.as_of:
        return "outcome_unavailable_at_as_of"
    if observation.label_available_at < observation.outcome_at:
        return "label_availability_precedes_outcome"
    if observation.universe_known_at > observation.prediction_at:
        return "historical_universe_unknown"
    if not observation.in_historical_universe:
        return "survivorship_contamination"
    return None
