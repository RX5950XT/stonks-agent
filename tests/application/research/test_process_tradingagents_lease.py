from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.adapters.research.tradingagents_http import TradingAgentsResultReceipt
from stonks_agent.application.research.process_tradingagents_lease import (
    process_tradingagents_lease,
)
from stonks_agent.domain.artifact import ArtifactMetadata
from stonks_agent.domain.errors import ErrorCode, Failure, StructuredError, Success
from stonks_agent.domain.job import (
    CompleteJob,
    JobCompletionReceipt,
    JobLease,
    QuarantinedWorkerResult,
)
from stonks_contracts.common import ConfidenceCalibration
from stonks_contracts.evidence import Sensitivity
from stonks_contracts.research import AgentOpinion, AnalysisBundle
from stonks_contracts.tradingagents import (
    SignedEvidenceArtifact,
    TradingAgentsWorkerRequest,
    TradingAgentsWorkerResponse,
    TradingAgentsWorkerResult,
)

NOW = datetime(2026, 7, 13, 2, tzinfo=UTC)
REQUEST_ID = UUID("20000000-0000-4000-8000-000000000001")
RUN_ID = UUID("20000000-0000-4000-8000-000000000002")
JOB_ID = UUID("20000000-0000-4000-8000-000000000003")
INSTRUMENT_ID = UUID("20000000-0000-4000-8000-000000000004")
EVIDENCE_ID = UUID("20000000-0000-4000-8000-000000000005")
OPINION_ID = UUID("20000000-0000-4000-8000-000000000006")


def lease(**overrides: object) -> JobLease:
    values: dict[str, object] = {
        "job_id": JOB_ID,
        "run_id": RUN_ID,
        "job_type": "tradingagents_research",
        "payload": {"request_id": str(REQUEST_ID)},
        "attempt_generation": 4,
        "attempt_nonce": "nonce-current",
        "lease_owner": "core-runner-1",
        "lease_until": NOW + timedelta(minutes=2),
        "attempts": 4,
        "deadline_at": NOW + timedelta(minutes=1),
    }
    values.update(overrides)
    return JobLease.model_validate(values)


def request(**overrides: object) -> TradingAgentsWorkerRequest:
    artifact_hash = "a" * 64
    values: dict[str, object] = {
        "request_id": REQUEST_ID,
        "run_id": RUN_ID,
        "job_id": JOB_ID,
        "attempt_generation": 4,
        "attempt_nonce": "nonce-current",
        "profile": "paper",
        "instrument_id": INSTRUMENT_ID,
        "symbol": "AAPL",
        "as_of": NOW - timedelta(minutes=1),
        "horizon": "20 trading days",
        "allowed_evidence_ids": (EVIDENCE_ID,),
        "evidence": (
            SignedEvidenceArtifact(
                evidence_id=EVIDENCE_ID,
                artifact_ref=f"sha256:{artifact_hash}",
                signed_url=(
                    f"http://artifact-service:8080/v1/artifacts/{artifact_hash}"
                    "?expires=1783905000&signature=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                ),
                expires_at=NOW + timedelta(minutes=5),
                available_at=NOW - timedelta(minutes=2),
                category="market",
                untrusted_content=True,
            ),
        ),
        "deadline": NOW + timedelta(minutes=1),
    }
    values.update(overrides)
    return TradingAgentsWorkerRequest.model_validate(values)


def result_receipt() -> TradingAgentsResultReceipt:
    result = TradingAgentsWorkerResult(
        analysis_bundle=AnalysisBundle(
            bundle_id=UUID("20000000-0000-4000-8000-000000000007"),
            run_id=RUN_ID,
            as_of=NOW - timedelta(minutes=1),
            analyst_artifact_ids=(),
            opinion_ids=(OPINION_ID,),
            source_refs=(EVIDENCE_ID,),
            worker_version="tradingagents-worker/0.1.0",
        ),
        agent_opinion=AgentOpinion(
            opinion_id=OPINION_ID,
            instrument_id=INSTRUMENT_ID,
            as_of=NOW - timedelta(minutes=1),
            horizon="20 trading days",
            recommendation="Hold",
            thesis="Scoped opinion",
            confidence=Decimal("0"),
            calibration=ConfidenceCalibration.UNCALIBRATED,
            evidence_refs=(EVIDENCE_ID,),
            producer="tradingagents-isolated-worker",
            model_version="pinned",
        ),
    )
    response = TradingAgentsWorkerResponse(
        request_id=REQUEST_ID,
        run_id=RUN_ID,
        job_id=JOB_ID,
        attempt_generation=4,
        attempt_nonce="nonce-current",
        result_artifact_hash=result.payload_hash(),
        result=result,
    )
    stored = MemoryArtifactStore().finalize(
        result.canonical_json().encode(),
        metadata=ArtifactMetadata(
            media_type="application/json",
            license_tag="Apache-2.0",
            sensitivity=Sensitivity.INTERNAL,
            source="tradingagents-isolated-worker",
        ),
        finalized_at=NOW,
    )
    assert isinstance(stored, Success)
    return TradingAgentsResultReceipt(response=response, artifact=stored.value)


class Worker:
    def __init__(self) -> None:
        self.requests: list[TradingAgentsWorkerRequest] = []

    def analyze(
        self, value: TradingAgentsWorkerRequest
    ) -> Success[TradingAgentsResultReceipt]:
        self.requests.append(value)
        return Success(result_receipt())


class Queue:
    def __init__(self, result: object) -> None:
        self.result = result
        self.completions: list[CompleteJob] = []

    def complete(
        self, value: CompleteJob, *, now: datetime, artifact: object = None
    ) -> object:
        assert now == NOW
        assert artifact == result_receipt().artifact
        self.completions.append(value)
        return self.result


class LateAudit:
    def __init__(self, failure: Failure | None = None) -> None:
        self.failure = failure
        self.records: list[QuarantinedWorkerResult] = []

    def record(self, value: QuarantinedWorkerResult) -> object:
        self.records.append(value)
        return self.failure or Success(value)


def completion() -> Success[JobCompletionReceipt]:
    return Success(
        JobCompletionReceipt(
            job_id=JOB_ID,
            run_id=RUN_ID,
            event_id=UUID("20000000-0000-4000-8000-000000000008"),
            outbox_id=UUID("20000000-0000-4000-8000-000000000009"),
            sequence=2,
            result_artifact_hash=result_receipt().response.result_artifact_hash,
            completed_at=NOW,
        )
    )


def test_only_core_queue_completion_can_ack_worker_result() -> None:
    worker, queue, audit = Worker(), Queue(completion()), LateAudit()

    actual = process_tradingagents_lease(
        lease(),
        request(),
        now=NOW,
        worker=worker,
        queue=queue,
        late_results=audit,  # type: ignore[arg-type]
    )

    assert isinstance(actual, Success)
    assert worker.requests == [request()]
    assert queue.completions == [
        CompleteJob(
            job_id=JOB_ID,
            worker_id="core-runner-1",
            attempt_generation=4,
            attempt_nonce="nonce-current",
            result_artifact_hash=result_receipt().response.result_artifact_hash,
        )
    ]
    assert audit.records == []


def test_invalid_initial_fence_never_calls_worker_or_completion() -> None:
    worker, queue, audit = Worker(), Queue(completion()), LateAudit()

    actual = process_tradingagents_lease(
        lease(),
        request(attempt_nonce="stale"),
        now=NOW,
        worker=worker,
        queue=queue,  # type: ignore[arg-type]
        late_results=audit,
    )

    assert isinstance(actual, Failure)
    assert actual.error.code is ErrorCode.CONFLICT
    assert worker.requests == []
    assert queue.completions == []
    assert audit.records == []


def test_db_rejected_late_result_goes_only_to_quarantine_audit() -> None:
    conflict = Failure(StructuredError(ErrorCode.CONFLICT, "lease was reclaimed"))
    worker, queue, audit = Worker(), Queue(conflict), LateAudit()

    actual = process_tradingagents_lease(
        lease(),
        request(),
        now=NOW,
        worker=worker,
        queue=queue,
        late_results=audit,  # type: ignore[arg-type]
    )

    assert actual is conflict
    assert len(queue.completions) == 1
    assert len(audit.records) == 1
    assert audit.records[0].reason == "stale_attempt"
    assert (
        audit.records[0].result_artifact_hash
        == result_receipt().response.result_artifact_hash
    )


def test_quarantine_audit_failure_fails_closed_and_non_conflict_is_not_quarantined() -> (
    None
):
    audit_failure = Failure(
        StructuredError(ErrorCode.INTERNAL_ERROR, "quarantine audit unavailable")
    )
    conflict = Failure(StructuredError(ErrorCode.CONFLICT, "lease was reclaimed"))
    audit = LateAudit(audit_failure)
    actual = process_tradingagents_lease(
        lease(),
        request(),
        now=NOW,
        worker=Worker(),
        queue=Queue(conflict),
        late_results=audit,  # type: ignore[arg-type]
    )
    assert actual is audit_failure

    audit = LateAudit()
    unavailable = Failure(StructuredError(ErrorCode.INTERNAL_ERROR, "DB unavailable"))
    actual = process_tradingagents_lease(
        lease(),
        request(),
        now=NOW,
        worker=Worker(),
        queue=Queue(unavailable),
        late_results=audit,  # type: ignore[arg-type]
    )
    assert actual is unavailable
    assert audit.records == []
