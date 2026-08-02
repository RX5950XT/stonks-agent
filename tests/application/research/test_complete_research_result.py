from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.application.research.complete_research_result import (
    complete_research_result,
)
from stonks_agent.domain.artifact import ArtifactMetadata
from stonks_agent.domain.errors import ErrorCode, Failure, StructuredError, Success
from stonks_agent.domain.job import (
    CompleteJob,
    JobCompletionReceipt,
    JobLease,
    QuarantinedWorkerResult,
)
from stonks_contracts.evidence import Sensitivity

NOW = datetime(2026, 7, 28, 9, tzinfo=UTC)
REQUEST_ID = UUID("76000000-0000-4000-8000-000000000001")
RUN_ID = UUID("76000000-0000-4000-8000-000000000002")
JOB_ID = UUID("76000000-0000-4000-8000-000000000003")


def _lease() -> JobLease:
    return JobLease(
        job_id=JOB_ID,
        run_id=RUN_ID,
        job_type="research_pipeline",
        payload={"request_id": str(REQUEST_ID)},
        attempt_generation=3,
        attempt_nonce="opaque-current-fence",
        lease_owner="research-worker-a",
        lease_until=NOW + timedelta(minutes=10),
        attempts=3,
        deadline_at=NOW + timedelta(minutes=5),
    )


def _manifest() -> object:
    stored = MemoryArtifactStore().finalize(
        b'{"schema_version":"1.0.0"}',
        metadata=ArtifactMetadata(
            media_type="application/json",
            license_tag="Apache-2.0",
            sensitivity=Sensitivity.INTERNAL,
            source="stonks-agent-research-worker",
            attributes=(
                ("run_id", str(RUN_ID)),
                ("schema", "research-worker-result/1.0.0"),
            ),
        ),
        finalized_at=NOW,
    )
    assert isinstance(stored, Success)
    return stored.value


class _Queue:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[CompleteJob] = []

    def complete(
        self,
        request: CompleteJob,
        *,
        now: datetime,
        artifact: object = None,
    ) -> object:
        assert now == NOW
        assert artifact == _manifest()
        self.calls.append(request)
        return self.result


class _LateAudit:
    def __init__(self, result: object | None = None) -> None:
        self.result = result
        self.records: list[QuarantinedWorkerResult] = []

    def record(self, value: QuarantinedWorkerResult) -> object:
        self.records.append(value)
        return self.result or Success(value)


def _receipt() -> Success[JobCompletionReceipt]:
    manifest = _manifest()
    return Success(
        JobCompletionReceipt(
            job_id=JOB_ID,
            run_id=RUN_ID,
            event_id=UUID("76000000-0000-4000-8000-000000000004"),
            outbox_id=UUID("76000000-0000-4000-8000-000000000005"),
            sequence=2,
            result_artifact_hash=manifest.content_hash,  # type: ignore[attr-defined]
            completed_at=NOW,
        )
    )


def test_research_completion_uses_exact_lease_fence_without_quarantine() -> None:
    queue, audit = _Queue(_receipt()), _LateAudit()

    result = complete_research_result(
        _lease(),
        request_id=REQUEST_ID,
        manifest=_manifest(),  # type: ignore[arg-type]
        now=NOW,
        queue=queue,  # type: ignore[arg-type]
        late_results=audit,  # type: ignore[arg-type]
    )

    assert result == _receipt()
    assert queue.calls[0].attempt_nonce == _lease().attempt_nonce
    assert audit.records == []


def test_conflicting_research_completion_is_recorded_only_as_late_audit() -> None:
    conflict = Failure(StructuredError(ErrorCode.CONFLICT, "lease was reclaimed"))
    queue, audit = _Queue(conflict), _LateAudit()

    result = complete_research_result(
        _lease(),
        request_id=REQUEST_ID,
        manifest=_manifest(),  # type: ignore[arg-type]
        now=NOW,
        queue=queue,  # type: ignore[arg-type]
        late_results=audit,  # type: ignore[arg-type]
    )

    assert result is conflict
    assert len(audit.records) == 1
    assert audit.records[0] == QuarantinedWorkerResult(
        job_id=JOB_ID,
        run_id=RUN_ID,
        request_id=REQUEST_ID,
        attempt_generation=3,
        result_artifact_hash=_manifest().content_hash,  # type: ignore[attr-defined]
        reason="stale_attempt",
        observed_at=NOW,
    )


def test_late_audit_failure_wins_and_non_conflict_is_not_quarantined() -> None:
    audit_failure = Failure(
        StructuredError(ErrorCode.INTERNAL_ERROR, "late audit unavailable")
    )
    conflict = Failure(StructuredError(ErrorCode.CONFLICT, "lease was reclaimed"))
    audit = _LateAudit(audit_failure)
    result = complete_research_result(
        _lease(),
        request_id=REQUEST_ID,
        manifest=_manifest(),  # type: ignore[arg-type]
        now=NOW,
        queue=_Queue(conflict),  # type: ignore[arg-type]
        late_results=audit,  # type: ignore[arg-type]
    )
    assert result is audit_failure

    unavailable = Failure(StructuredError(ErrorCode.INTERNAL_ERROR, "DB unavailable"))
    audit = _LateAudit()
    result = complete_research_result(
        _lease(),
        request_id=REQUEST_ID,
        manifest=_manifest(),  # type: ignore[arg-type]
        now=NOW,
        queue=_Queue(unavailable),  # type: ignore[arg-type]
        late_results=audit,  # type: ignore[arg-type]
    )
    assert result is unavailable
    assert audit.records == []
