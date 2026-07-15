"""Authority-free contracts for RD-Agent-compatible candidate sandboxing."""

from __future__ import annotations

import hashlib
from enum import StrEnum
from itertools import pairwise
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from .common import (
    ArtifactRef,
    ContractModel,
    DecimalString,
    NonEmptyString,
    PositiveDecimal,
    Sha256,
    UTCDateTime,
    canonical_json,
    stable_payload_hash,
)

REQUIRED_EVALUATION_CHECKS = (
    "point_in_time",
    "leakage",
    "survivorship",
    "reproducibility",
    "baseline_comparison",
    "cost_sensitivity",
    "drawdown",
    "calibration",
    "overfitting",
)


class RDAgentCandidateKind(StrEnum):
    FACTOR = "factor"


class CandidateSandboxPolicy(ContractModel):
    policy_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}/[0-9]+$")
    platform: Literal["linux"]
    network_mode: Literal["none"]
    root_filesystem: Literal["read_only"]
    dataset_mount: Literal["read_only"]
    entrypoint: Literal["compute"]
    repetitions: Literal[2]
    max_source_bytes: int = Field(ge=256, le=1_000_000)
    max_ast_nodes: int = Field(ge=16, le=100_000)
    max_rows: int = Field(ge=2, le=1_000_000)
    max_output_bytes: int = Field(ge=1_024, le=16_777_216)
    timeout_seconds: int = Field(ge=1, le=600)
    memory_megabytes: int = Field(ge=64, le=16_384)
    cpu_cores: PositiveDecimal
    max_processes: Literal[1]
    max_open_files: int = Field(ge=8, le=1_024)
    max_log_bytes: int = Field(ge=0, le=1_048_576)
    writable_tmpfs_megabytes: int = Field(ge=1, le=1_024)
    run_as_uid: Literal[65532]
    run_as_gid: Literal[65532]
    privileged: Literal[False]
    capability_mode: Literal["drop_all"]
    no_new_privileges: Literal[True]
    seccomp_profile: Literal["runtime/default"]
    apparmor_profile: Literal["docker-default"]
    device_access: Literal["none"]
    host_namespace_mode: Literal["private"]
    unix_socket_access: Literal["none"]
    environment_mode: Literal["allowlist"]
    source_mount: Literal["read_only"]
    output_tmpfs_options: Literal["rw,noexec,nosuid,nodev"]
    fixed_argv: Literal[True]
    shell_allowed: Literal[False]
    isolation_scope: Literal["fresh_container_per_repetition"]
    python_hash_seed: Literal[0]
    allowed_calls: tuple[str, ...] = Field(min_length=1, max_length=64)
    forbidden_names: tuple[str, ...] = Field(min_length=1, max_length=128)
    core_static_rescan_required: Literal[True]
    core_full_evaluation_required: Literal[True]
    promotion_allowed: Literal[False]

    @model_validator(mode="after")
    def validate_names(self) -> Self:
        for values in (self.allowed_calls, self.forbidden_names):
            if values != tuple(sorted(set(values))) or any(
                not value.isidentifier() for value in values
            ):
                raise ValueError("candidate sandbox names must be unique and sorted")
        if set(self.allowed_calls) & set(self.forbidden_names):
            raise ValueError("candidate sandbox allowed and forbidden names overlap")
        return self

    @property
    def policy_hash(self) -> str:
        return self.payload_hash()


class CandidateScanResult(ContractModel):
    source_hash: Sha256
    policy_hash: Sha256
    ast_hash: Sha256
    node_count: int = Field(ge=1, le=100_000)
    entrypoint: Literal["compute"]
    accepted: Literal[True]
    scan_hash: Sha256

    @classmethod
    def create(cls, **values: object) -> CandidateScanResult:
        draft = cls.model_construct(**values, scan_hash="0" * 64)  # type: ignore[arg-type]
        expected = stable_payload_hash(draft.model_dump(mode="json", exclude={"scan_hash"}))
        return cls.model_validate(values | {"scan_hash": expected})

    @model_validator(mode="after")
    def validate_scan_hash(self) -> Self:
        expected = stable_payload_hash(self.model_dump(mode="json", exclude={"scan_hash"}))
        if self.scan_hash != expected:
            raise ValueError("candidate scan hash mismatch")
        return self


class RDAgentProposal(ContractModel):
    proposal_id: UUID
    candidate_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}/[0-9]+\.[0-9]+\.[0-9]+$")
    candidate_kind: RDAgentCandidateKind
    rd_agent_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    proposal_artifact_ref: ArtifactRef
    generation_runtime_hash: Sha256
    generation_config_hash: Sha256
    generation_input_artifact_ref: ArtifactRef
    raw_generation_artifact_ref: ArtifactRef
    source: str = Field(min_length=1, max_length=1_000_000, repr=False)
    source_hash: Sha256
    source_artifact_ref: ArtifactRef
    generated_at: UTCDateTime
    untrusted_content: Literal[True] = True
    authority: Literal["draft_only"] = "draft_only"

    @classmethod
    def create(cls, **values: object) -> RDAgentProposal:
        source = values.get("source")
        if not isinstance(source, str):
            raise ValueError("RD-Agent proposal source is required")
        source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        payload = values | {
            "source_hash": source_hash,
            "source_artifact_ref": f"sha256:{source_hash}",
        }
        draft = cls.model_construct(
            **payload,  # type: ignore[arg-type]
            proposal_artifact_ref="sha256:" + "0" * 64,
        )
        proposal_hash = stable_payload_hash(
            draft.model_dump(mode="json", exclude={"proposal_artifact_ref"})
        )
        return cls.model_validate(payload | {"proposal_artifact_ref": f"sha256:{proposal_hash}"})

    @property
    def proposal_payload_hash(self) -> str:
        return stable_payload_hash(self.model_dump(mode="json", exclude={"proposal_artifact_ref"}))

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        digest = hashlib.sha256(self.source.encode("utf-8")).hexdigest()
        if (
            len(self.source.encode("utf-8")) > 1_000_000
            or self.source_hash != digest
            or self.source_artifact_ref != f"sha256:{digest}"
            or self.proposal_artifact_ref != f"sha256:{self.proposal_payload_hash}"
        ):
            raise ValueError("RD-Agent proposal artifact or source hash mismatch")
        return self


class RDSandboxDatasetRow(ContractModel):
    observation_id: UUID
    instrument_id: UUID
    event_at: UTCDateTime
    feature_available_at: UTCDateTime
    prediction_at: UTCDateTime
    features: tuple[DecimalString, ...] = Field(min_length=1, max_length=32)


class RDSandboxDataset(ContractModel):
    dataset_snapshot_id: UUID
    source_data_hash: Sha256
    as_of: UTCDateTime
    feature_spec_hash: Sha256
    label_spec_hash: Sha256
    universe_spec_hash: Sha256
    cost_model_hash: Sha256
    split_policy_hash: Sha256
    rows: tuple[RDSandboxDatasetRow, ...] = Field(
        min_length=2,
        max_length=1_000_000,
    )

    @model_validator(mode="after")
    def validate_rows(self) -> Self:
        keys = tuple(
            (item.prediction_at, item.instrument_id.hex, item.observation_id.hex)
            for item in self.rows
        )
        if (
            any(current <= previous for previous, current in pairwise(keys))
            or len({item.observation_id for item in self.rows}) != len(self.rows)
            or len({len(item.features) for item in self.rows}) != 1
            or any(
                not (item.event_at <= item.feature_available_at <= item.prediction_at <= self.as_of)
                for item in self.rows
            )
        ):
            raise ValueError("RD-Agent sandbox dataset is not PIT aligned")
        return self


class RDSandboxRuntimeIdentity(ContractModel):
    worker_version: NonEmptyString = Field(max_length=128)
    adapter_version: NonEmptyString = Field(max_length=128)
    rd_agent_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    rd_agent_source_hash: Sha256
    runtime_hash: Sha256
    image_digest: ArtifactRef
    python_version: NonEmptyString = Field(max_length=64)
    deterministic: Literal[True]


class RDSandboxJob(ContractModel):
    request_id: UUID
    run_id: UUID
    job_id: UUID
    attempt_generation: int = Field(ge=1)
    attempt_nonce: NonEmptyString = Field(max_length=128, repr=False)
    execution_mode: Literal["sandbox"]
    proposal: RDAgentProposal
    dataset_artifact_ref: ArtifactRef
    dataset: RDSandboxDataset
    evaluation_policy_hash: Sha256
    sandbox_policy_hash: Sha256
    runtime: RDSandboxRuntimeIdentity
    requested_at: UTCDateTime
    deadline: UTCDateTime
    promotion_allowed: Literal[False]

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        if not (
            self.dataset_artifact_ref == f"sha256:{self.dataset.payload_hash()}"
            and self.proposal.rd_agent_commit == self.runtime.rd_agent_commit
            and self.dataset.as_of <= self.proposal.generated_at <= self.requested_at
            and self.requested_at < self.deadline
        ):
            raise ValueError("RD-Agent sandbox job binding is invalid")
        return self


class RDSandboxInvocation(ContractModel):
    sandbox_instance_id: UUID
    job: RDSandboxJob


class SandboxPrediction(ContractModel):
    observation_id: UUID
    predicted_return: DecimalString


def sandbox_prediction_hash(values: tuple[SandboxPrediction, ...]) -> str:
    return stable_payload_hash([item.model_dump(mode="json") for item in values])


def sandbox_prediction_byte_count(values: tuple[SandboxPrediction, ...]) -> int:
    payload = [item.model_dump(mode="json") for item in values]
    return len(canonical_json(payload).encode("utf-8"))


class DraftArtifactKind(StrEnum):
    SOURCE = "source"
    PREDICTIONS = "predictions"
    STATIC_SCAN = "static_scan"


class DraftArtifact(ContractModel):
    kind: DraftArtifactKind
    content_hash: Sha256
    byte_count: int = Field(ge=1, le=16_777_216)
    media_type: Literal["text/x-python", "application/json"]
    draft_only: Literal[True]


class DraftEvaluationRequest(ContractModel):
    candidate_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}/[0-9]+\.[0-9]+\.[0-9]+$")
    source_hash: Sha256
    prediction_hash: Sha256
    dataset_snapshot_id: UUID
    source_data_hash: Sha256
    as_of: UTCDateTime
    feature_spec_hash: Sha256
    label_spec_hash: Sha256
    universe_spec_hash: Sha256
    cost_model_hash: Sha256
    split_policy_hash: Sha256
    evaluation_policy_hash: Sha256
    required_checks: tuple[str, ...] = Field(min_length=9, max_length=9)
    core_full_evaluation_required: Literal[True]
    promotion_allowed: Literal[False]

    @model_validator(mode="after")
    def validate_checks(self) -> Self:
        if self.required_checks != REQUIRED_EVALUATION_CHECKS:
            raise ValueError("RD-Agent draft requires the exact evaluation checks")
        return self

    @property
    def request_hash(self) -> str:
        return self.payload_hash()


class RDSandboxRunResult(ContractModel):
    sandbox_instance_id: UUID
    proposal_id: UUID
    candidate_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}/[0-9]+\.[0-9]+\.[0-9]+$")
    runtime: RDSandboxRuntimeIdentity
    sandbox_policy_hash: Sha256
    scan: CandidateScanResult
    source_artifact: DraftArtifact
    prediction_artifact: DraftArtifact
    predictions: tuple[SandboxPrediction, ...] = Field(
        min_length=2,
        max_length=1_000_000,
    )
    output_hash: Sha256
    process_isolation: Literal["fresh_container"]
    generated_at: UTCDateTime

    @model_validator(mode="after")
    def validate_run(self) -> Self:
        prediction_hash = sandbox_prediction_hash(self.predictions)
        identities = tuple(item.observation_id for item in self.predictions)
        valid = (
            len(identities) == len(set(identities))
            and self.output_hash == prediction_hash
            and self.source_artifact.kind is DraftArtifactKind.SOURCE
            and self.scan.source_hash == self.source_artifact.content_hash
            and self.scan.policy_hash == self.sandbox_policy_hash
            and self.prediction_artifact.kind is DraftArtifactKind.PREDICTIONS
            and self.prediction_artifact.content_hash == prediction_hash
            and self.prediction_artifact.byte_count
            == sandbox_prediction_byte_count(self.predictions)
        )
        if not valid:
            raise ValueError("RD-Agent sandbox run artifact is not bound")
        return self


class RDSandboxRunResponse(ContractModel):
    request_id: UUID
    run_id: UUID
    job_id: UUID
    attempt_generation: int = Field(ge=1)
    attempt_nonce: NonEmptyString = Field(max_length=128, repr=False)
    result_artifact_hash: Sha256
    result: RDSandboxRunResult

    @model_validator(mode="after")
    def validate_result_hash(self) -> Self:
        if self.result_artifact_hash != self.result.payload_hash():
            raise ValueError("RD-Agent sandbox run artifact hash mismatch")
        return self

    def validate_against(self, job: RDSandboxJob) -> None:
        fence = (
            self.request_id,
            self.run_id,
            self.job_id,
            self.attempt_generation,
            self.attempt_nonce,
        )
        expected_fence = (
            job.request_id,
            job.run_id,
            job.job_id,
            job.attempt_generation,
            job.attempt_nonce,
        )
        valid = (
            fence == expected_fence
            and self.result.proposal_id == job.proposal.proposal_id
            and self.result.candidate_id == job.proposal.candidate_id
            and self.result.runtime == job.runtime
            and self.result.sandbox_policy_hash == job.sandbox_policy_hash
            and self.result.source_artifact.content_hash == job.proposal.source_hash
            and self.result.source_artifact.byte_count == len(job.proposal.source.encode("utf-8"))
            and tuple(item.observation_id for item in self.result.predictions)
            == tuple(item.observation_id for item in job.dataset.rows)
            and job.requested_at <= self.result.generated_at <= job.deadline
        )
        if not valid:
            raise ValueError("RD-Agent sandbox run fence or binding changed")


class RDSandboxResult(ContractModel):
    proposal_id: UUID
    candidate_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}/[0-9]+\.[0-9]+\.[0-9]+$")
    runtime: RDSandboxRuntimeIdentity
    sandbox_policy_hash: Sha256
    scan: CandidateScanResult
    source_artifact: DraftArtifact
    prediction_artifact: DraftArtifact
    predictions: tuple[SandboxPrediction, ...] = Field(
        min_length=2,
        max_length=1_000_000,
    )
    first_sandbox_instance_id: UUID
    replay_sandbox_instance_id: UUID
    first_output_hash: Sha256
    replay_output_hash: Sha256
    draft_evaluation_request: DraftEvaluationRequest
    deterministic: Literal[True]
    generated_at: UTCDateTime

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        prediction_hash = sandbox_prediction_hash(self.predictions)
        identities = tuple(item.observation_id for item in self.predictions)
        valid = (
            len(set(identities)) == len(identities)
            and self.first_sandbox_instance_id != self.replay_sandbox_instance_id
            and self.first_output_hash == self.replay_output_hash
            and self.first_output_hash == prediction_hash
            and self.source_artifact.kind is DraftArtifactKind.SOURCE
            and self.prediction_artifact.kind is DraftArtifactKind.PREDICTIONS
            and self.prediction_artifact.content_hash == prediction_hash
            and self.prediction_artifact.byte_count
            == sandbox_prediction_byte_count(self.predictions)
            and self.draft_evaluation_request.candidate_id == self.candidate_id
            and self.draft_evaluation_request.source_hash == self.source_artifact.content_hash
            and self.draft_evaluation_request.prediction_hash == prediction_hash
            and self.scan.source_hash == self.source_artifact.content_hash
            and self.scan.policy_hash == self.sandbox_policy_hash
        )
        if not valid:
            raise ValueError("RD-Agent sandbox result is not reproducible or bound")
        return self


class RDSandboxWorkerResponse(ContractModel):
    request_id: UUID
    run_id: UUID
    job_id: UUID
    attempt_generation: int = Field(ge=1)
    attempt_nonce: NonEmptyString = Field(max_length=128, repr=False)
    result_artifact_hash: Sha256
    result: RDSandboxResult

    @model_validator(mode="after")
    def validate_result_hash(self) -> Self:
        if self.result_artifact_hash != self.result.payload_hash():
            raise ValueError("RD-Agent sandbox result artifact hash mismatch")
        return self

    def validate_against(self, job: RDSandboxJob) -> None:
        draft = self.result.draft_evaluation_request
        fence = (
            self.request_id,
            self.run_id,
            self.job_id,
            self.attempt_generation,
            self.attempt_nonce,
        )
        expected_fence = (
            job.request_id,
            job.run_id,
            job.job_id,
            job.attempt_generation,
            job.attempt_nonce,
        )
        valid = (
            fence == expected_fence
            and self.result.proposal_id == job.proposal.proposal_id
            and self.result.candidate_id == job.proposal.candidate_id
            and self.result.runtime == job.runtime
            and self.result.sandbox_policy_hash == job.sandbox_policy_hash
            and self.result.source_artifact.content_hash == job.proposal.source_hash
            and self.result.source_artifact.byte_count == len(job.proposal.source.encode("utf-8"))
            and tuple(item.observation_id for item in self.result.predictions)
            == tuple(item.observation_id for item in job.dataset.rows)
            and draft.dataset_snapshot_id == job.dataset.dataset_snapshot_id
            and draft.source_data_hash == job.dataset.source_data_hash
            and draft.as_of == job.dataset.as_of
            and draft.feature_spec_hash == job.dataset.feature_spec_hash
            and draft.label_spec_hash == job.dataset.label_spec_hash
            and draft.universe_spec_hash == job.dataset.universe_spec_hash
            and draft.cost_model_hash == job.dataset.cost_model_hash
            and draft.split_policy_hash == job.dataset.split_policy_hash
            and draft.evaluation_policy_hash == job.evaluation_policy_hash
            and job.requested_at <= self.result.generated_at <= job.deadline
        )
        if not valid:
            raise ValueError("RD-Agent sandbox response fence or binding changed")
