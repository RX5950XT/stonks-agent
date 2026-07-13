"""Core-owned fenced completion for isolated TradingAgents research."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from stonks_agent.adapters.research.tradingagents_http import TradingAgentsResultReceipt
from stonks_agent.domain.errors import ErrorCode, Failure, Result, StructuredError
from stonks_agent.domain.job import (
    CompleteJob,
    JobCompletionReceipt,
    JobLease,
    QuarantinedWorkerResult,
)
from stonks_agent.ports.late_result_audit import LateResultAuditPort
from stonks_agent.ports.queue import QueuePort
from stonks_contracts.tradingagents import TradingAgentsWorkerRequest


class TradingAgentsAnalysisPort(Protocol):
    def analyze(
        self, request: TradingAgentsWorkerRequest
    ) -> Result[TradingAgentsResultReceipt]: ...


def process_tradingagents_lease(
    lease: JobLease,
    request: TradingAgentsWorkerRequest,
    *,
    now: datetime,
    worker: TradingAgentsAnalysisPort,
    queue: QueuePort,
    late_results: LateResultAuditPort,
) -> Result[JobCompletionReceipt]:
    """Call the worker, then let the core DB transaction revalidate and ack."""

    invalid = _request_fence_failure(lease, request, now)
    if invalid is not None:
        return invalid
    analyzed = worker.analyze(request)
    if isinstance(analyzed, Failure):
        return analyzed
    response = analyzed.value.response
    completed = queue.complete(
        CompleteJob(
            job_id=lease.job_id,
            worker_id=lease.lease_owner,
            attempt_generation=response.attempt_generation,
            attempt_nonce=response.attempt_nonce,
            result_artifact_hash=response.result_artifact_hash,
        ),
        now=now,
        artifact=analyzed.value.artifact,
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
            request_id=request.request_id,
            attempt_generation=response.attempt_generation,
            result_artifact_hash=response.result_artifact_hash,
            reason="stale_attempt",
            observed_at=now,
        )
    )
    return quarantined if isinstance(quarantined, Failure) else completed


def _request_fence_failure(
    lease: JobLease,
    request: TradingAgentsWorkerRequest,
    now: datetime,
) -> Failure | None:
    invalid = (
        now.tzinfo is None
        or lease.job_type != "tradingagents_research"
        or lease.job_id != request.job_id
        or lease.run_id != request.run_id
        or lease.attempt_generation != request.attempt_generation
        or lease.attempt_nonce != request.attempt_nonce
        or lease.deadline_at != request.deadline
        or lease.lease_until <= now
        or lease.deadline_at <= now
    )
    if invalid:
        return Failure(
            StructuredError(
                code=ErrorCode.CONFLICT,
                message="TradingAgents lease fence is stale or invalid",
            )
        )
    return None
