"""Fail-closed one-shot adapter for factor-expression candidates."""

from __future__ import annotations

import hashlib
import logging
import platform
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stonks_contracts.candidate_scan import scan_candidate_source
from stonks_contracts.common import stable_payload_hash
from stonks_contracts.rd_agent import (
    CandidateSandboxPolicy,
    DraftArtifact,
    DraftArtifactKind,
    RDSandboxDatasetRow,
    RDSandboxJob,
    RDSandboxRunResponse,
    RDSandboxRunResult,
    RDSandboxRuntimeIdentity,
    SandboxPrediction,
    sandbox_prediction_byte_count,
    sandbox_prediction_hash,
)

LOGGER = logging.getLogger(__name__)
RUNTIME_FILES = (
    "CVE_REVIEW.md",
    "Dockerfile",
    "NOTICE.md",
    "adapter.py",
    "candidate_runner.py",
    "cli.py",
    "distribution-manifest.yaml",
    "grype.yaml",
    "openvex.json",
    "pyproject.toml",
    "runner.py",
    "sandbox_policy.yaml",
    "uv.lock",
)
type CandidateErrorCode = Literal[
    "candidate_timeout",
    "candidate_memory_exceeded",
    "candidate_output_too_large",
    "candidate_process_failed",
]


class WorkerError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    message: str = Field(min_length=1, max_length=256)


class WorkerFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    error: WorkerError


class WorkerSuccess(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: RDSandboxRunResponse


type WorkerOutcome = WorkerSuccess | WorkerFailure


class CandidateRunOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    predictions: tuple[SandboxPrediction, ...] = Field(
        min_length=2,
        max_length=1_000_000,
    )


class CandidateProcessError(Exception):
    def __init__(self, code: CandidateErrorCode, detail: str = "") -> None:
        super().__init__(detail)
        self.code = code


class CandidateProcessRunner(Protocol):
    def run(
        self,
        *,
        source: str,
        rows: tuple[RDSandboxDatasetRow, ...],
        policy: CandidateSandboxPolicy,
    ) -> CandidateRunOutput: ...


class SandboxWorkerPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime: RDSandboxRuntimeIdentity
    sandbox: CandidateSandboxPolicy


class RDAgentSandboxWorker:
    def __init__(
        self,
        *,
        policy: SandboxWorkerPolicy,
        runner: CandidateProcessRunner,
        clock: Callable[[], datetime] | None = None,
        platform_name: Callable[[], str] | None = None,
    ) -> None:
        self.policy = policy
        self.runner = runner
        self._clock = clock or (lambda: datetime.now(UTC))
        self._platform_name = platform_name or platform.system

    def run(self, job: RDSandboxJob, *, sandbox_instance_id: UUID) -> WorkerOutcome:
        checked = self._validate_job(job)
        if isinstance(checked, WorkerFailure):
            return checked
        try:
            scan = scan_candidate_source(job.proposal.source, self.policy.sandbox)
        except ValueError:
            return _failure("candidate_rejected", "Candidate source was rejected")
        try:
            output = self.runner.run(
                source=job.proposal.source,
                rows=job.dataset.rows,
                policy=self.policy.sandbox,
            )
            return self._build_success(job, sandbox_instance_id, scan, output)
        except CandidateProcessError as error:
            return _failure(error.code, _safe_runner_message(error.code))
        except (OSError, ValidationError, ValueError) as error:
            LOGGER.error("candidate sandbox failed error_type=%s", type(error).__name__)
            return _failure("candidate_output_invalid", "Candidate output was invalid")
        except Exception as error:
            LOGGER.error("candidate sandbox failed error_type=%s", type(error).__name__)
            return _failure("sandbox_failed", "Candidate sandbox failed")

    def _validate_job(self, job: RDSandboxJob) -> WorkerFailure | None:
        try:
            job = RDSandboxJob.model_validate(job.model_dump(mode="python"))
        except ValidationError:
            return _failure("invalid_job", "RD candidate job is invalid")
        now = self._clock()
        if self._platform_name() != "Linux":
            return _failure("platform_denied", "RD candidate sandbox requires Linux")
        if now.tzinfo is None or now >= job.deadline:
            return _failure("deadline_expired", "RD candidate deadline expired")
        if job.runtime != self.policy.runtime:
            return _failure(
                "runtime_mismatch", "RD candidate runtime identity mismatch"
            )
        if job.sandbox_policy_hash != self.policy.sandbox.policy_hash:
            return _failure(
                "sandbox_policy_mismatch",
                "RD candidate sandbox policy mismatch",
            )
        if len(job.dataset.rows) > self.policy.sandbox.max_rows:
            return _failure("dataset_too_large", "RD candidate dataset exceeds limit")
        return None

    def _build_success(
        self,
        job: RDSandboxJob,
        instance_id: UUID,
        scan: object,
        output: CandidateRunOutput,
    ) -> WorkerOutcome:
        expected_ids = tuple(item.observation_id for item in job.dataset.rows)
        actual_ids = tuple(item.observation_id for item in output.predictions)
        byte_count = sandbox_prediction_byte_count(output.predictions)
        if (
            actual_ids != expected_ids
            or byte_count > self.policy.sandbox.max_output_bytes
        ):
            return _failure("candidate_output_invalid", "Candidate output was invalid")
        result = _run_result(
            job=job,
            instance_id=instance_id,
            scan=scan,
            predictions=output.predictions,
            generated_at=self._clock(),
        )
        if not job.requested_at <= result.generated_at <= job.deadline:
            return _failure("deadline_expired", "RD candidate deadline expired")
        response = RDSandboxRunResponse(
            request_id=job.request_id,
            run_id=job.run_id,
            job_id=job.job_id,
            attempt_generation=job.attempt_generation,
            attempt_nonce=job.attempt_nonce,
            result_artifact_hash=result.payload_hash(),
            result=result,
        )
        return WorkerSuccess(value=response)


def _run_result(
    *,
    job: RDSandboxJob,
    instance_id: UUID,
    scan: object,
    predictions: tuple[SandboxPrediction, ...],
    generated_at: datetime,
) -> RDSandboxRunResult:
    from stonks_contracts.rd_agent import CandidateScanResult

    checked_scan = CandidateScanResult.model_validate(scan)
    prediction_hash = sandbox_prediction_hash(predictions)
    return RDSandboxRunResult(
        sandbox_instance_id=instance_id,
        proposal_id=job.proposal.proposal_id,
        candidate_id=job.proposal.candidate_id,
        runtime=job.runtime,
        sandbox_policy_hash=job.sandbox_policy_hash,
        scan=checked_scan,
        source_artifact=DraftArtifact(
            kind=DraftArtifactKind.SOURCE,
            content_hash=job.proposal.source_hash,
            byte_count=len(job.proposal.source.encode("utf-8")),
            media_type="text/x-python",
            draft_only=True,
        ),
        prediction_artifact=DraftArtifact(
            kind=DraftArtifactKind.PREDICTIONS,
            content_hash=prediction_hash,
            byte_count=sandbox_prediction_byte_count(predictions),
            media_type="application/json",
            draft_only=True,
        ),
        predictions=predictions,
        output_hash=prediction_hash,
        process_isolation="fresh_container",
        generated_at=generated_at,
    )


def _safe_runner_message(code: CandidateErrorCode) -> str:
    return {
        "candidate_timeout": "Candidate execution timed out",
        "candidate_memory_exceeded": "Candidate exceeded its memory limit",
        "candidate_output_too_large": "Candidate output exceeded its limit",
        "candidate_process_failed": "Candidate process failed",
    }[code]


def _failure(code: str, message: str) -> WorkerFailure:
    return WorkerFailure(error=WorkerError(code=code, message=message))


def compute_runtime_hash(worker_root: Path) -> str:
    identities = tuple(
        {
            "path": relative_path,
            "sha256": hashlib.sha256(
                (worker_root / relative_path).read_bytes()
            ).hexdigest(),
        }
        for relative_path in RUNTIME_FILES
    )
    return stable_payload_hash(identities)
