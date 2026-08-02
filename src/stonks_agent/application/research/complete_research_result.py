"""Fenced completion and quarantine audit for local research results."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from stonks_agent.domain.errors import ErrorCode, Failure, Result
from stonks_agent.domain.job import (
    CompleteJob,
    JobCompletionReceipt,
    JobLease,
    QuarantinedWorkerResult,
)
from stonks_agent.ports.artifact_store import ArtifactManifest
from stonks_agent.ports.late_result_audit import LateResultAuditPort
from stonks_agent.ports.queue import QueuePort


def complete_research_result(
    lease: JobLease,
    *,
    request_id: UUID,
    manifest: ArtifactManifest,
    now: datetime,
    queue: QueuePort,
    late_results: LateResultAuditPort,
) -> Result[JobCompletionReceipt]:
    """Commit through the DB fence or preserve the rejected result as audit only."""

    completed = queue.complete(
        CompleteJob(
            job_id=lease.job_id,
            worker_id=lease.lease_owner,
            attempt_generation=lease.attempt_generation,
            attempt_nonce=lease.attempt_nonce,
            result_artifact_hash=manifest.content_hash,
        ),
        now=now,
        artifact=manifest,
    )
    if (
        not isinstance(completed, Failure)
        or completed.error.code is not ErrorCode.CONFLICT
    ):
        return completed
    quarantined = late_results.record(
        QuarantinedWorkerResult(
            job_id=lease.job_id,
            run_id=lease.run_id,
            request_id=request_id,
            attempt_generation=lease.attempt_generation,
            result_artifact_hash=manifest.content_hash,
            reason="stale_attempt",
            observed_at=now,
        )
    )
    return quarantined if isinstance(quarantined, Failure) else completed
