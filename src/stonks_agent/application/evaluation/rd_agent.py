"""Trusted aggregation and full evaluation for RD factor drafts."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from pydantic import ValidationError

from stonks_agent.application.evaluation.contracts import (
    CandidatePredictionSeries,
    EvaluationDataset,
    EvaluationPolicy,
)
from stonks_agent.application.evaluation.promotion import evaluate_for_promotion
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.evaluation import EvaluationReport, EvaluationRequest
from stonks_agent.domain.strategy import StrategyKind
from stonks_contracts.candidate_scan import scan_candidate_source
from stonks_contracts.common import ArtifactRef, UTCDateTime
from stonks_contracts.rd_agent import (
    REQUIRED_EVALUATION_CHECKS,
    CandidateSandboxPolicy,
    DraftEvaluationRequest,
    RDSandboxJob,
    RDSandboxResult,
    RDSandboxRunResponse,
    RDSandboxWorkerResponse,
)


def aggregate_sandbox_runs(
    *,
    job: RDSandboxJob,
    first: RDSandboxRunResponse,
    replay: RDSandboxRunResponse,
    sandbox_policy: CandidateSandboxPolicy,
) -> Result[RDSandboxWorkerResponse]:
    try:
        first.validate_against(job)
        replay.validate_against(job)
        scan = scan_candidate_source(job.proposal.source, sandbox_policy)
        if not _runs_match(job, first, replay, sandbox_policy, scan):
            return _invalid("Fresh RD candidate sandboxes were not reproducible")
        draft = _draft_request(job, first.result.output_hash)
        result = RDSandboxResult(
            proposal_id=job.proposal.proposal_id,
            candidate_id=job.proposal.candidate_id,
            runtime=job.runtime,
            sandbox_policy_hash=job.sandbox_policy_hash,
            scan=scan,
            source_artifact=first.result.source_artifact,
            prediction_artifact=first.result.prediction_artifact,
            predictions=first.result.predictions,
            first_sandbox_instance_id=first.result.sandbox_instance_id,
            replay_sandbox_instance_id=replay.result.sandbox_instance_id,
            first_output_hash=first.result.output_hash,
            replay_output_hash=replay.result.output_hash,
            draft_evaluation_request=draft,
            deterministic=True,
            generated_at=max(first.result.generated_at, replay.result.generated_at),
        )
        response = _aggregate_response(job, result)
        response.validate_against(job)
        return Success(response)
    except (ValidationError, ValueError):
        return _invalid("RD candidate sandbox receipts failed validation")


def evaluate_rd_agent_candidate(
    *,
    job: RDSandboxJob,
    response: RDSandboxWorkerResponse,
    sandbox_policy: CandidateSandboxPolicy,
    request: EvaluationRequest,
    dataset: EvaluationDataset,
    baselines: tuple[CandidatePredictionSeries, ...],
    policy: EvaluationPolicy,
    report_id: UUID,
    report_artifact_ref: ArtifactRef,
    created_at: UTCDateTime,
) -> Result[EvaluationReport]:
    try:
        response.validate_against(job)
        scan = scan_candidate_source(job.proposal.source, sandbox_policy)
    except (ValidationError, ValueError):
        return _invalid("RD candidate response failed core revalidation")
    if not _evaluation_binding_is_valid(
        job, response, sandbox_policy, request, dataset, policy, scan
    ):
        return _invalid("RD candidate evaluation binding changed")
    candidate_dataset = _candidate_dataset(dataset, response)
    return evaluate_for_promotion(
        request=request,
        dataset=candidate_dataset,
        baselines=baselines,
        policy=policy,
        report_id=report_id,
        report_artifact_ref=report_artifact_ref,
        created_at=created_at,
    )


def _runs_match(
    job: RDSandboxJob,
    first: RDSandboxRunResponse,
    replay: RDSandboxRunResponse,
    policy: CandidateSandboxPolicy,
    scan: object,
) -> bool:
    return (
        job.sandbox_policy_hash == policy.policy_hash
        and first.result.sandbox_instance_id != replay.result.sandbox_instance_id
        and first.result.process_isolation == "fresh_container"
        and replay.result.process_isolation == "fresh_container"
        and first.result.scan == scan == replay.result.scan
        and first.result.output_hash == replay.result.output_hash
        and first.result.predictions == replay.result.predictions
        and first.result.prediction_artifact == replay.result.prediction_artifact
        and first.result.prediction_artifact.byte_count <= policy.max_output_bytes
    )


def _draft_request(job: RDSandboxJob, prediction_hash: str) -> DraftEvaluationRequest:
    data = job.dataset
    return DraftEvaluationRequest(
        candidate_id=job.proposal.candidate_id,
        source_hash=job.proposal.source_hash,
        prediction_hash=prediction_hash,
        dataset_snapshot_id=data.dataset_snapshot_id,
        source_data_hash=data.source_data_hash,
        as_of=data.as_of,
        feature_spec_hash=data.feature_spec_hash,
        label_spec_hash=data.label_spec_hash,
        universe_spec_hash=data.universe_spec_hash,
        cost_model_hash=data.cost_model_hash,
        split_policy_hash=data.split_policy_hash,
        evaluation_policy_hash=job.evaluation_policy_hash,
        required_checks=REQUIRED_EVALUATION_CHECKS,
        core_full_evaluation_required=True,
        promotion_allowed=False,
    )


def _aggregate_response(
    job: RDSandboxJob, result: RDSandboxResult
) -> RDSandboxWorkerResponse:
    return RDSandboxWorkerResponse(
        request_id=job.request_id,
        run_id=job.run_id,
        job_id=job.job_id,
        attempt_generation=job.attempt_generation,
        attempt_nonce=job.attempt_nonce,
        result_artifact_hash=result.payload_hash(),
        result=result,
    )


def _evaluation_binding_is_valid(
    job: RDSandboxJob,
    response: RDSandboxWorkerResponse,
    sandbox_policy: CandidateSandboxPolicy,
    request: EvaluationRequest,
    dataset: EvaluationDataset,
    policy: EvaluationPolicy,
    scan: object,
) -> bool:
    manifest = request.manifest
    draft = response.result.draft_evaluation_request
    strategy_id, version = job.proposal.candidate_id.rsplit("/", 1)
    return (
        job.sandbox_policy_hash == sandbox_policy.policy_hash
        and response.result.scan == scan
        and request.requested_at >= job.requested_at
        and request.dataset_snapshot_id == dataset.dataset_snapshot_id
        and request.dataset_snapshot_id == job.dataset.dataset_snapshot_id
        and request.data_hash == dataset.data_hash == job.dataset.source_data_hash
        and request.as_of == dataset.as_of == job.dataset.as_of
        and request.evaluation_policy_hash == policy.policy_hash
        and request.evaluation_policy_hash == job.evaluation_policy_hash
        and manifest.strategy_id == strategy_id
        and manifest.strategy_version == version
        and manifest.kind is StrategyKind.QUANT_MODEL
        and manifest.source_artifact_ref == job.proposal.source_artifact_ref
        and manifest.runtime_hash == job.runtime.runtime_hash
        and manifest.feature_spec_hash == job.dataset.feature_spec_hash
        and manifest.label_spec_hash == job.dataset.label_spec_hash
        and manifest.universe_spec_hash == job.dataset.universe_spec_hash
        and manifest.cost_model_hash == job.dataset.cost_model_hash
        and manifest.split_policy_hash == job.dataset.split_policy_hash
        and manifest.parameters_hash == job.proposal.proposal_payload_hash
        and manifest.deterministic is True
        and _rows_align(job, dataset)
        and draft.required_checks == REQUIRED_EVALUATION_CHECKS
    )


def _rows_align(job: RDSandboxJob, dataset: EvaluationDataset) -> bool:
    if len(job.dataset.rows) != len(dataset.observations):
        return False
    return all(
        (
            source.observation_id,
            source.instrument_id,
            source.event_at,
            source.feature_available_at,
            source.prediction_at,
        )
        == (
            target.observation_id,
            target.instrument_id,
            target.event_at,
            target.feature_available_at,
            target.prediction_at,
        )
        for source, target in zip(job.dataset.rows, dataset.observations, strict=True)
    )


def _candidate_dataset(
    dataset: EvaluationDataset,
    response: RDSandboxWorkerResponse,
) -> EvaluationDataset:
    previous: dict[UUID, Decimal] = {}
    observations = []
    for value, prediction in zip(
        dataset.observations, response.result.predictions, strict=True
    ):
        exposure = _exposure(prediction.predicted_return)
        turnover = abs(exposure - previous.get(value.instrument_id, Decimal(0)))
        previous[value.instrument_id] = exposure
        observations.append(
            value.model_copy(
                update={
                    "predicted_return": prediction.predicted_return,
                    "direction_probability": _probability(exposure),
                    "turnover": turnover,
                }
            )
        )
    return dataset.model_copy(update={"observations": tuple(observations)})


def _exposure(value: Decimal) -> Decimal:
    if value > 0:
        return Decimal(1)
    if value < 0:
        return Decimal(-1)
    return Decimal(0)


def _probability(exposure: Decimal) -> Decimal:
    if exposure > 0:
        return Decimal(1)
    if exposure < 0:
        return Decimal(0)
    return Decimal("0.5")


def _invalid(message: str) -> Failure:
    return Failure(
        StructuredError(code=ErrorCode.MODEL_OUTPUT_INVALID, message=message)
    )
