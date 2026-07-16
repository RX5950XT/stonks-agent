from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, text

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.adapters.postgres.job_queue import PostgresJobQueue
from stonks_agent.adapters.postgres.snapshot_completion import (
    PostgresSnapshotCompletionStore,
)
from stonks_agent.adapters.postgres.snapshot_requests import (
    PostgresSnapshotRequestStore,
)
from stonks_agent.application.data.process_snapshot_lease import (
    process_snapshot_lease,
)
from stonks_agent.domain.data_quality import ProviderDataState, ProviderObservation
from stonks_agent.domain.dataset_snapshot import (
    MaterializedEvidence,
    ProviderSnapshotMaterialization,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.evidence import AvailabilityCertainty, EvidenceTimeline
from stonks_agent.domain.job import JobLease
from stonks_agent.domain.provider_policy import ProviderPolicy, ProviderRoute
from stonks_agent.domain.snapshot import CreateSnapshotRequest
from stonks_agent.ports.artifact_store import ArtifactManifest
from stonks_contracts.evidence import Sensitivity

pytestmark = pytest.mark.postgres
AS_OF = datetime(2026, 1, 2, 21, tzinfo=UTC)


@pytest.fixture
def db_now(clean_database: Engine) -> datetime:
    with clean_database.connect() as connection:
        value = connection.scalar(text("select clock_timestamp()"))
    assert isinstance(value, datetime)
    return value


class StubSource:
    def __init__(self, result: Result[ProviderSnapshotMaterialization]) -> None:
        self.result = result
        self.calls = 0

    def fetch(
        self,
        request: object,
        *,
        provider_policy_id: str,
    ) -> Result[ProviderSnapshotMaterialization]:
        self.calls += 1
        return self.result


class CountingArtifactStore(MemoryArtifactStore):
    def __init__(self) -> None:
        super().__init__()
        self.finalize_calls = 0

    def finalize(
        self,
        content: object,
        *,
        metadata: object,
        finalized_at: object,
    ) -> Result[ArtifactManifest]:
        self.finalize_calls += 1
        return super().finalize(
            content,
            metadata=metadata,
            finalized_at=finalized_at,
        )


def test_db_clock_rejects_expired_lease_despite_stale_caller_time(
    clean_database: Engine,
    db_now: datetime,
) -> None:
    caller_now = db_now - timedelta(minutes=10)
    request = snapshot_request("db-clock-expired", requested_at=caller_now)
    PostgresSnapshotRequestStore(clean_database).submit(request)
    lease = unwrap(
        PostgresJobQueue(clean_database).claim(
            worker_id="worker-a",
            now=caller_now,
            lease_for=timedelta(minutes=1),
        )
    )
    expired = db_now - timedelta(seconds=1)
    with clean_database.begin() as connection:
        connection.execute(
            text("update job set lease_until = :expired where job_id = :job_id"),
            {"expired": expired, "job_id": lease.job_id},
        )
    lease = lease.model_copy(update={"lease_until": expired})
    source = StubSource(Success(materialization()))
    artifacts = CountingArtifactStore()

    result = process_snapshot_lease(
        lease,
        now=caller_now + timedelta(seconds=1),
        source=source,
        artifacts=artifacts,
        completions=PostgresSnapshotCompletionStore(clean_database),
        policy=provider_policy(),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    assert source.calls == 0
    assert artifacts.finalize_calls == 0
    assert lifecycle_counts(clean_database) == (0, 0, 0, 0, 0, 0, 0)


@pytest.mark.parametrize(
    "tamper_sql",
    (
        "update job set payload = jsonb_set(payload, '{query,symbol}', '\"MSFT\"')",
        "update job set payload_hash = repeat('f', 64)",
        "update job set lease_owner = 'rogue-worker'",
        "update job set attempt_generation = attempt_generation + 1",
        "update job set attempt_nonce = 'rogue-nonce'",
        "update job set lease_until = :expired",
        "update job set deadline_at = deadline_at - interval '1 second'",
        "update run set policy_id = 'rogue-policy/1'",
        "update run set input_hash = repeat('e', 64)",
        "update run set as_of = as_of + interval '1 day'",
    ),
)
def test_preflight_rejects_tampered_db_authority_before_external_io(
    clean_database: Engine,
    db_now: datetime,
    tamper_sql: str,
) -> None:
    request = snapshot_request("preflight-tamper", requested_at=db_now)
    PostgresSnapshotRequestStore(clean_database).submit(request)
    lease = unwrap(
        PostgresJobQueue(clean_database).claim(
            worker_id="worker-a",
            now=db_now,
            lease_for=timedelta(minutes=5),
        )
    )
    with clean_database.begin() as connection:
        connection.execute(
            text(tamper_sql), {"expired": db_now - timedelta(milliseconds=1)}
        )
    source = StubSource(Success(materialization()))
    artifacts = MemoryArtifactStore()

    result = process_snapshot_lease(
        lease,
        now=db_now - timedelta(days=1),
        source=source,
        artifacts=artifacts,
        completions=PostgresSnapshotCompletionStore(clean_database),
        policy=provider_policy(),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    assert source.calls == 0
    assert not artifacts.is_finalized(hashlib.sha256(b"raw").hexdigest())
    assert lifecycle_counts(clean_database) == (0, 0, 0, 0, 0, 0, 0)


@pytest.mark.parametrize(
    ("provider", "endpoint"),
    (("rogue", "/v1/prices"), ("replay", "/rogue")),
)
def test_rogue_source_has_zero_artifact_and_canonical_rows_but_is_audited(
    clean_database: Engine,
    db_now: datetime,
    provider: str,
    endpoint: str,
) -> None:
    request = snapshot_request(f"rogue-{provider}-{endpoint}", requested_at=db_now)
    lease = submitted_lease(clean_database, request, now=db_now)
    artifacts = MemoryArtifactStore()

    result = process_snapshot_lease(
        lease,
        now=db_now,
        source=StubSource(
            Success(materialization(provider=provider, endpoint=endpoint))
        ),
        artifacts=artifacts,
        completions=PostgresSnapshotCompletionStore(clean_database),
        policy=provider_policy(),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CAPABILITY_DENIED
    assert not artifacts.is_finalized(hashlib.sha256(b"raw").hexdigest())
    assert lifecycle_counts(clean_database) == (0, 0, 0, 0, 0, 1, 1)
    assert job_audit_state(clean_database) == (
        "queued",
        None,
        "running",
        "snapshot.retry_scheduled",
        "snapshot.retry_scheduled",
        {"code": "capability_denied", "stage": "provider", "attempt_generation": 1},
    )


def test_provider_failure_is_atomically_requeued_and_audited(
    clean_database: Engine,
    db_now: datetime,
) -> None:
    request = snapshot_request("provider-failure", requested_at=db_now)
    lease = submitted_lease(clean_database, request, now=db_now)
    provider_failure = Failure(
        StructuredError(
            code=ErrorCode.DATA_UNAVAILABLE,
            message="provider unavailable; do-not-store-detail",
        )
    )

    result = process_snapshot_lease(
        lease,
        now=db_now,
        source=StubSource(provider_failure),
        artifacts=MemoryArtifactStore(),
        completions=PostgresSnapshotCompletionStore(clean_database),
        policy=provider_policy(),
    )

    assert result is provider_failure
    assert job_audit_state(clean_database) == (
        "queued",
        None,
        "running",
        "snapshot.retry_scheduled",
        "snapshot.retry_scheduled",
        {"code": "data_unavailable", "stage": "provider", "attempt_generation": 1},
    )
    with clean_database.connect() as connection:
        payload = connection.scalar(text("select payload::text from run_event"))
    assert "do-not-store-detail" not in str(payload)


def test_materialization_failure_requeues_without_canonical_db_rows(
    clean_database: Engine,
    db_now: datetime,
) -> None:
    request = snapshot_request("materialization-failure", requested_at=db_now)
    lease = submitted_lease(clean_database, request, now=db_now)
    artifacts = MemoryArtifactStore()

    result = process_snapshot_lease(
        lease,
        now=db_now,
        source=StubSource(Success(failed_materialization())),
        artifacts=artifacts,
        completions=PostgresSnapshotCompletionStore(clean_database),
        policy=provider_policy(),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.DATA_UNAVAILABLE
    assert not artifacts.is_finalized(hashlib.sha256(b"raw").hexdigest())
    assert lifecycle_counts(clean_database) == (0, 0, 0, 0, 0, 1, 1)
    assert job_audit_state(clean_database)[0:5] == (
        "queued",
        None,
        "running",
        "snapshot.retry_scheduled",
        "snapshot.retry_scheduled",
    )
    assert job_audit_state(clean_database)[5]["stage"] == "materialization"


def test_second_preflight_rejects_lease_expiry_before_artifact_write(
    clean_database: Engine,
    db_now: datetime,
) -> None:
    request = snapshot_request("completion-toctou", requested_at=db_now)
    lease = submitted_lease(clean_database, request, now=db_now)
    artifacts = MemoryArtifactStore()

    result = process_snapshot_lease(
        lease,
        now=db_now,
        source=LeaseExpiringSuccessSource(clean_database, db_now),
        artifacts=artifacts,
        completions=PostgresSnapshotCompletionStore(clean_database),
        policy=provider_policy(),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    assert not artifacts.is_finalized(hashlib.sha256(b"raw").hexdigest())
    assert lifecycle_counts(clean_database) == (0, 0, 0, 0, 0, 0, 0)
    with clean_database.connect() as connection:
        state = connection.execute(
            text("select j.status, j.lease_owner, j.lease_until from job j")
        ).one()
    assert state == (
        "leased",
        "worker-a",
        db_now - timedelta(milliseconds=1),
    )


def test_failure_uses_db_clock_and_cannot_audit_an_expired_lease(
    clean_database: Engine,
    db_now: datetime,
) -> None:
    caller_now = db_now - timedelta(minutes=10)
    request = snapshot_request("failure-db-clock", requested_at=caller_now)
    lease = submitted_lease(clean_database, request, now=caller_now)

    result = process_snapshot_lease(
        lease,
        now=caller_now + timedelta(seconds=1),
        source=LeaseExpiringFailureSource(clean_database, db_now),
        artifacts=MemoryArtifactStore(),
        completions=PostgresSnapshotCompletionStore(clean_database),
        policy=provider_policy(),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    assert lifecycle_counts(clean_database) == (0, 0, 0, 0, 0, 0, 0)
    with clean_database.connect() as connection:
        state = connection.execute(
            text("select status, lease_owner, lease_until from job")
        ).one()
    assert state == ("leased", "worker-a", db_now - timedelta(milliseconds=1))


def test_failure_after_takeover_is_stale_and_cannot_commit_audit(
    clean_database: Engine,
    db_now: datetime,
) -> None:
    request = snapshot_request("stale-failure", requested_at=db_now)
    PostgresSnapshotRequestStore(clean_database).submit(request)
    queue = PostgresJobQueue(clean_database)
    stale = unwrap(
        queue.claim(
            worker_id="worker-a",
            now=db_now,
            lease_for=timedelta(seconds=1),
        )
    )
    source = TakeoverFailureSource(queue, clean_database, db_now)

    result = process_snapshot_lease(
        stale,
        now=db_now,
        source=source,
        artifacts=MemoryArtifactStore(),
        completions=PostgresSnapshotCompletionStore(clean_database),
        policy=provider_policy(),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    assert source.current is not None
    assert source.current.attempt_generation == stale.attempt_generation + 1
    assert lifecycle_counts(clean_database) == (0, 0, 0, 0, 0, 0, 0)
    with clean_database.connect() as connection:
        state = connection.execute(text("select status, lease_owner from job")).one()
    assert state == ("leased", "worker-b")


def test_exhausted_failure_atomically_dead_letters_job_and_run(
    clean_database: Engine,
    db_now: datetime,
) -> None:
    request = snapshot_request("terminal-failure", requested_at=db_now)
    lease = submitted_lease(clean_database, request, now=db_now)
    with clean_database.begin() as connection:
        connection.execute(text("update job set max_attempts = attempts"))

    result = process_snapshot_lease(
        lease,
        now=db_now,
        source=StubSource(
            Failure(
                StructuredError(
                    code=ErrorCode.DATA_UNAVAILABLE,
                    message="provider unavailable",
                )
            )
        ),
        artifacts=MemoryArtifactStore(),
        completions=PostgresSnapshotCompletionStore(clean_database),
        policy=provider_policy(),
    )

    assert isinstance(result, Failure)
    assert job_audit_state(clean_database)[0:5] == (
        "dead_letter",
        None,
        "failed",
        "snapshot.failed",
        "snapshot.failed",
    )


def test_requeued_failure_can_be_reclaimed_and_complete_on_new_fence(
    clean_database: Engine,
    db_now: datetime,
) -> None:
    request = snapshot_request("retry-then-success", requested_at=db_now)
    first = submitted_lease(clean_database, request, now=db_now)
    store = PostgresSnapshotCompletionStore(clean_database)
    process_snapshot_lease(
        first,
        now=db_now,
        source=StubSource(
            Failure(
                StructuredError(
                    code=ErrorCode.DATA_UNAVAILABLE,
                    message="temporary provider outage",
                )
            )
        ),
        artifacts=MemoryArtifactStore(),
        completions=store,
        policy=provider_policy(),
    )
    second = unwrap(
        PostgresJobQueue(clean_database).claim(
            worker_id="worker-b",
            now=db_now + timedelta(seconds=1),
            lease_for=timedelta(minutes=5),
        )
    )

    receipt = unwrap(
        process_snapshot_lease(
            second,
            now=db_now + timedelta(seconds=1),
            source=StubSource(Success(materialization())),
            artifacts=MemoryArtifactStore(),
            completions=store,
            policy=provider_policy(),
        )
    )

    assert second.attempt_generation == first.attempt_generation + 1
    assert receipt.sequence == 3
    with clean_database.connect() as connection:
        state = connection.execute(
            text("select j.status, r.status from job j join run r using (run_id)")
        ).one()
        events = (
            connection.execute(
                text("select event_type from run_event order by sequence")
            )
            .scalars()
            .all()
        )
        topics = (
            connection.execute(text("select topic from outbox order by sequence"))
            .scalars()
            .all()
        )
    assert state == ("succeeded", "succeeded")
    assert events == ["snapshot.retry_scheduled", "snapshot.completed"]
    assert topics == events


def test_failure_audit_storage_error_rolls_back_job_and_run_transition(
    clean_database: Engine,
    db_now: datetime,
) -> None:
    request = snapshot_request("failure-rollback", requested_at=db_now)
    lease = submitted_lease(clean_database, request, now=db_now)
    with clean_database.begin() as connection:
        connection.execute(
            text(
                "alter table outbox add constraint test_force_failure_rollback check (false)"
            )
        )
    try:
        result = process_snapshot_lease(
            lease,
            now=db_now,
            source=StubSource(
                Failure(
                    StructuredError(
                        code=ErrorCode.DATA_UNAVAILABLE,
                        message="temporary provider outage",
                    )
                )
            ),
            artifacts=MemoryArtifactStore(),
            completions=PostgresSnapshotCompletionStore(clean_database),
            policy=provider_policy(),
        )
    finally:
        with clean_database.begin() as connection:
            connection.execute(
                text("alter table outbox drop constraint test_force_failure_rollback")
            )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    assert lifecycle_counts(clean_database) == (0, 0, 0, 0, 0, 0, 0)
    with clean_database.connect() as connection:
        state = connection.execute(
            text(
                "select j.status, j.lease_owner, r.status from job j join run r using (run_id)"
            )
        ).one()
    assert state == ("leased", "worker-a", "pending")


class TakeoverFailureSource:
    def __init__(
        self,
        queue: PostgresJobQueue,
        engine: Engine,
        now: datetime,
    ) -> None:
        self.queue = queue
        self.engine = engine
        self.now = now
        self.current: JobLease | None = None

    def fetch(
        self,
        request: object,
        *,
        provider_policy_id: str,
    ) -> Result[ProviderSnapshotMaterialization]:
        with self.engine.begin() as connection:
            connection.execute(
                text("update job set lease_until = :expired"),
                {"expired": self.now - timedelta(seconds=1)},
            )
        self.current = unwrap(
            self.queue.claim(
                worker_id="worker-b",
                now=self.now + timedelta(seconds=2),
                lease_for=timedelta(minutes=5),
            )
        )
        return Failure(
            StructuredError(
                code=ErrorCode.DATA_UNAVAILABLE,
                message="stale provider failure",
            )
        )


class LeaseExpiringSuccessSource:
    def __init__(self, engine: Engine, now: datetime) -> None:
        self.engine = engine
        self.now = now

    def fetch(
        self,
        request: object,
        *,
        provider_policy_id: str,
    ) -> Result[ProviderSnapshotMaterialization]:
        with self.engine.begin() as connection:
            connection.execute(
                text("update job set lease_until = :expired"),
                {"expired": self.now - timedelta(milliseconds=1)},
            )
        return Success(materialization())


class LeaseExpiringFailureSource:
    def __init__(self, engine: Engine, now: datetime) -> None:
        self.engine = engine
        self.now = now

    def fetch(
        self,
        request: object,
        *,
        provider_policy_id: str,
    ) -> Result[ProviderSnapshotMaterialization]:
        with self.engine.begin() as connection:
            connection.execute(
                text("update job set lease_until = :expired"),
                {"expired": self.now - timedelta(milliseconds=1)},
            )
        return Failure(
            StructuredError(
                code=ErrorCode.DATA_UNAVAILABLE,
                message="provider failed after the lease expired",
            )
        )


def submitted_lease(
    engine: Engine,
    request: CreateSnapshotRequest,
    *,
    now: datetime,
) -> JobLease:
    PostgresSnapshotRequestStore(engine).submit(request)
    return unwrap(
        PostgresJobQueue(engine).claim(
            worker_id="worker-a",
            now=now,
            lease_for=timedelta(minutes=5),
        )
    )


def snapshot_request(key: str, *, requested_at: datetime) -> CreateSnapshotRequest:
    return CreateSnapshotRequest(
        market="US",
        capability="prices",
        as_of=AS_OF,
        query={"symbol": "AAPL"},
        provider_policy_id="us-prices/1",
        idempotency_key=key,
        owner_subject="test-owner",
        requested_at=requested_at,
    )


def provider_policy() -> ProviderPolicy:
    return ProviderPolicy(
        policy_id="us-prices/1",
        market="US",
        capability="prices",
        routes=(
            ProviderRoute(
                provider="replay",
                origin="https://replay.local",
                endpoints=("/v1/prices",),
                freshness_seconds=0,
                quota_floor=0,
            ),
        ),
        reconciliation_threshold=Decimal("0.01"),
    )


def materialization(
    *,
    provider: str = "replay",
    endpoint: str = "/v1/prices",
) -> ProviderSnapshotMaterialization:
    timeline = EvidenceTimeline(
        event_time=AS_OF - timedelta(minutes=1),
        published_at=AS_OF - timedelta(seconds=30),
        available_at=AS_OF,
        observed_at=AS_OF,
        as_of=AS_OF,
        availability_certainty=AvailabilityCertainty.PROVEN,
    )
    return ProviderSnapshotMaterialization(
        provider=provider,
        provider_version="fixture/1",
        endpoint=endpoint,
        raw_payload=b"raw",
        raw_media_type="application/json",
        license_tag="CC0-1.0",
        redistribution_tag="synthetic-unrestricted",
        sensitivity=Sensitivity.PUBLIC,
        observation=ProviderObservation[object](
            state=ProviderDataState.AVAILABLE,
            data=({"close": "100.00"},),
            completeness=Decimal("1"),
            observed_at=AS_OF,
        ),
        evidence=(
            MaterializedEvidence(
                subject="AAPL",
                kind="market_data",
                payload={"close": "100.00"},
                timeline=timeline,
            ),
        ),
    )


def failed_materialization() -> ProviderSnapshotMaterialization:
    return ProviderSnapshotMaterialization(
        provider="replay",
        provider_version="fixture/1",
        endpoint="/v1/prices",
        raw_payload=b"raw",
        raw_media_type="application/json",
        license_tag="CC0-1.0",
        redistribution_tag="synthetic-unrestricted",
        sensitivity=Sensitivity.PUBLIC,
        observation=ProviderObservation[object](
            state=ProviderDataState.FETCH_FAILED,
            data=(),
            completeness=Decimal("0"),
            observed_at=AS_OF,
            reasons=("provider_failed",),
        ),
        evidence=(),
    )


def lifecycle_counts(engine: Engine) -> tuple[int, ...]:
    tables = (
        "artifact_manifest",
        "evidence_item",
        "dataset_snapshot",
        "dataset_snapshot_evidence",
        "run_dataset_snapshot",
        "run_event",
        "outbox",
    )
    with engine.connect() as connection:
        return tuple(
            int(connection.scalar(text(f"select count(*) from {table}")) or 0)
            for table in tables
        )


def job_audit_state(engine: Engine) -> tuple[object, ...]:
    with engine.connect() as connection:
        return connection.execute(
            text(
                """
                select j.status, j.lease_owner, r.status, e.event_type, o.topic,
                       j.last_error
                from job j
                join run r on r.run_id = j.run_id
                join run_event e on e.run_id = r.run_id
                join outbox o on o.aggregate_id = r.run_id::text
                """
            )
        ).one()


def unwrap[T](result: Result[T]) -> T:
    assert isinstance(result, Success)
    return result.value
