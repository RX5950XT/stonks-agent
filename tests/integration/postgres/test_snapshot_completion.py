from __future__ import annotations

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
from stonks_agent.application.data.complete_snapshot import complete_snapshot
from stonks_agent.application.data.materialize_snapshot import materialize_snapshot
from stonks_agent.domain.data_quality import ProviderDataState, ProviderObservation
from stonks_agent.domain.dataset_snapshot import (
    MaterializedEvidence,
    MaterializedSnapshot,
    ProviderSnapshotMaterialization,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_agent.domain.evidence import AvailabilityCertainty, EvidenceTimeline
from stonks_agent.domain.job import CompleteJob, JobLease
from stonks_agent.domain.provider_policy import ProviderPolicy, ProviderRoute
from stonks_agent.domain.snapshot import CompleteSnapshotJob, CreateSnapshotRequest
from stonks_contracts.evidence import Sensitivity

pytestmark = pytest.mark.postgres
EVIDENCE_AS_OF = datetime(2026, 1, 2, 21, tzinfo=UTC)


def test_valid_fenced_completion_atomically_writes_canonical_snapshot(
    clean_database: Engine,
) -> None:
    request = snapshot_request(clean_database)
    refs = unwrap(PostgresSnapshotRequestStore(clean_database).submit(request))
    lease = unwrap(
        PostgresJobQueue(clean_database).claim(
            worker_id="worker-a",
            now=request.requested_at,
            lease_for=timedelta(minutes=5),
        )
    )
    artifacts = MemoryArtifactStore()
    snapshot = unwrap(materialize_snapshot(request, materialization(), artifacts))
    database_window_start = database_now(clean_database)

    result = complete_snapshot(
        completion(lease, snapshot),
        now=EVIDENCE_AS_OF + timedelta(hours=1),
        artifacts=artifacts,
        completions=PostgresSnapshotCompletionStore(clean_database),
        policy=provider_policy(),
    )
    database_window_end = database_now(clean_database)

    receipt = unwrap(result)
    assert refs.snapshot_id is None
    assert receipt.snapshot_id == snapshot.snapshot_id
    assert receipt.evidence_refs == snapshot.evidence_refs
    assert table_counts(clean_database) == (2, 1, 1, 1, 1, 1, 1)
    with clean_database.connect() as connection:
        state = connection.execute(
            text(
                """
                select j.status, j.result_artifact_hash, r.status,
                       e.event_type, o.topic
                from job j
                join run r on r.run_id = j.run_id
                join run_event e on e.run_id = r.run_id
                join outbox o on o.aggregate_id = r.run_id::text
                """
            )
        ).one()
    assert state == (
        "succeeded",
        snapshot.manifest_artifact_hash,
        "succeeded",
        "snapshot.completed",
        "snapshot.completed",
    )
    assert database_window_start <= receipt.completed_at <= database_window_end
    assert canonical_commit_times(clean_database) == (receipt.completed_at,) * 9


def test_stale_attempt_cannot_leave_any_canonical_snapshot_rows(
    clean_database: Engine,
) -> None:
    request = snapshot_request(clean_database)
    PostgresSnapshotRequestStore(clean_database).submit(request)
    queue = PostgresJobQueue(clean_database)
    stale = unwrap(
        queue.claim(
            worker_id="worker-a",
            now=request.requested_at,
            lease_for=timedelta(minutes=5),
        )
    )
    with clean_database.begin() as connection:
        connection.execute(
            text(
                """
                update job
                set lease_until = clock_timestamp() - interval '1 second'
                where job_id = :job_id
                """
            ),
            {"job_id": stale.job_id},
        )
    current = unwrap(
        queue.claim(
            worker_id="worker-b",
            now=request.requested_at,
            lease_for=timedelta(minutes=5),
        )
    )
    artifacts = MemoryArtifactStore()
    snapshot = unwrap(materialize_snapshot(request, materialization(), artifacts))

    result = complete_snapshot(
        completion(stale, snapshot),
        now=request.requested_at,
        artifacts=artifacts,
        completions=PostgresSnapshotCompletionStore(clean_database),
        policy=provider_policy(),
    )

    assert current.attempt_generation == stale.attempt_generation + 1
    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    assert table_counts(clean_database) == (0, 0, 0, 0, 0, 0, 0)


def test_database_expired_lease_rejects_stale_caller_without_any_mutation(
    clean_database: Engine,
) -> None:
    request = snapshot_request(clean_database)
    PostgresSnapshotRequestStore(clean_database).submit(request)
    lease = unwrap(
        PostgresJobQueue(clean_database).claim(
            worker_id="worker-a",
            now=request.requested_at,
            lease_for=timedelta(minutes=5),
        )
    )
    artifacts = MemoryArtifactStore()
    snapshot = unwrap(materialize_snapshot(request, materialization(), artifacts))
    with clean_database.begin() as connection:
        expired_lease = connection.scalar(
            text(
                """
                update job
                set lease_until = clock_timestamp() - interval '1 second'
                where job_id = :job_id
                returning lease_until
                """
            ),
            {"job_id": lease.job_id},
        )
    assert isinstance(expired_lease, datetime)
    before = job_run_state(clean_database)

    result = complete_snapshot(
        completion(lease, snapshot),
        now=expired_lease - timedelta(seconds=1),
        artifacts=artifacts,
        completions=PostgresSnapshotCompletionStore(clean_database),
        policy=provider_policy(),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    assert table_counts(clean_database) == (0, 0, 0, 0, 0, 0, 0)
    assert job_run_state(clean_database) == before


def test_generic_job_completion_cannot_bypass_snapshot_transaction(
    clean_database: Engine,
) -> None:
    request = snapshot_request(clean_database)
    PostgresSnapshotRequestStore(clean_database).submit(request)
    queue = PostgresJobQueue(clean_database)
    lease = unwrap(
        queue.claim(
            worker_id="worker-a",
            now=request.requested_at,
            lease_for=timedelta(minutes=5),
        )
    )

    result = queue.complete(
        CompleteJob(
            job_id=lease.job_id,
            worker_id=lease.lease_owner,
            attempt_generation=lease.attempt_generation,
            attempt_nonce=lease.attempt_nonce,
            result_artifact_hash="a" * 64,
        ),
        now=request.requested_at + timedelta(seconds=1),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CAPABILITY_DENIED
    assert table_counts(clean_database) == (0, 0, 0, 0, 0, 0, 0)


def test_completion_after_deadline_cannot_leave_canonical_rows(
    clean_database: Engine,
) -> None:
    request = snapshot_request(clean_database)
    PostgresSnapshotRequestStore(clean_database).submit(request)
    lease = unwrap(
        PostgresJobQueue(clean_database).claim(
            worker_id="worker-a",
            now=request.requested_at,
            lease_for=timedelta(minutes=30),
        )
    )
    artifacts = MemoryArtifactStore()
    snapshot = unwrap(materialize_snapshot(request, materialization(), artifacts))
    with clean_database.begin() as connection:
        expired_deadline = connection.scalar(
            text(
                """
                update job
                set deadline_at = not_before + interval '1 microsecond'
                where job_id = :job_id
                returning deadline_at
                """
            ),
            {"job_id": lease.job_id},
        )
    assert isinstance(expired_deadline, datetime)

    result = complete_snapshot(
        completion(lease, snapshot),
        now=expired_deadline - timedelta(seconds=1),
        artifacts=artifacts,
        completions=PostgresSnapshotCompletionStore(clean_database),
        policy=provider_policy(),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    assert table_counts(clean_database) == (0, 0, 0, 0, 0, 0, 0)


def test_exact_retry_is_idempotent_but_conflicting_result_fails_closed(
    clean_database: Engine,
) -> None:
    request = snapshot_request(clean_database)
    PostgresSnapshotRequestStore(clean_database).submit(request)
    lease = unwrap(
        PostgresJobQueue(clean_database).claim(
            worker_id="worker-a",
            now=request.requested_at,
            lease_for=timedelta(minutes=5),
        )
    )
    artifacts = MemoryArtifactStore()
    snapshot = unwrap(materialize_snapshot(request, materialization(), artifacts))
    command = completion(lease, snapshot)
    store = PostgresSnapshotCompletionStore(clean_database)

    first = unwrap(
        complete_snapshot(
            command,
            now=request.requested_at + timedelta(seconds=1),
            artifacts=artifacts,
            completions=store,
            policy=provider_policy(),
        )
    )
    retried = unwrap(
        complete_snapshot(
            command,
            now=request.requested_at + timedelta(seconds=2),
            artifacts=artifacts,
            completions=store,
            policy=provider_policy(),
        )
    )
    conflicting = unwrap(
        materialize_snapshot(
            request,
            materialization(raw=b'{"symbol":"AAPL","revision":2}'),
            artifacts,
        )
    )
    rejected = complete_snapshot(
        completion(lease, conflicting),
        now=request.requested_at + timedelta(seconds=3),
        artifacts=artifacts,
        completions=store,
        policy=provider_policy(),
    )

    assert retried == first
    assert isinstance(rejected, Failure)
    assert rejected.error.code is ErrorCode.CONFLICT
    assert table_counts(clean_database) == (2, 1, 1, 1, 1, 1, 1)


def test_second_run_can_reuse_canonical_snapshot_and_retry_exactly(
    clean_database: Engine,
) -> None:
    store = PostgresSnapshotCompletionStore(clean_database)
    artifacts = MemoryArtifactStore()
    first_request = snapshot_request(
        clean_database, idempotency_key="snapshot-reuse-first"
    )
    first_command = _complete_new_snapshot_run(
        clean_database,
        first_request,
        artifacts,
        store,
        completed_at=first_request.requested_at + timedelta(seconds=1),
    )
    second_request = snapshot_request(
        clean_database, idempotency_key="snapshot-reuse-second"
    )
    second_command = _complete_new_snapshot_run(
        clean_database,
        second_request,
        artifacts,
        store,
        completed_at=second_request.requested_at + timedelta(seconds=1),
    )

    retried = complete_snapshot(
        second_command,
        now=second_request.requested_at + timedelta(seconds=2),
        artifacts=artifacts,
        completions=store,
        policy=provider_policy(),
    )

    assert isinstance(retried, Success)
    assert first_command.snapshot.snapshot_id == second_command.snapshot.snapshot_id
    assert retried.value.snapshot_id == second_command.snapshot.snapshot_id
    assert table_counts(clean_database) == (2, 1, 1, 1, 2, 2, 2)


def test_retry_rejects_tampered_append_only_audit_content(
    clean_database: Engine,
) -> None:
    request = snapshot_request(clean_database)
    PostgresSnapshotRequestStore(clean_database).submit(request)
    lease = unwrap(
        PostgresJobQueue(clean_database).claim(
            worker_id="worker-a",
            now=request.requested_at,
            lease_for=timedelta(minutes=5),
        )
    )
    artifacts = MemoryArtifactStore()
    snapshot = unwrap(materialize_snapshot(request, materialization(), artifacts))
    command = completion(lease, snapshot)
    store = PostgresSnapshotCompletionStore(clean_database)
    unwrap(
        complete_snapshot(
            command,
            now=request.requested_at + timedelta(seconds=1),
            artifacts=artifacts,
            completions=store,
            policy=provider_policy(),
        )
    )
    with clean_database.begin() as connection:
        connection.execute(text("set local session_replication_role = replica"))
        connection.execute(
            text(
                """
                update run_event
                set event_type = 'forged.snapshot',
                    payload = '{}'::jsonb,
                    event_hash = :forged_hash
                """
            ),
            {"forged_hash": "f" * 64},
        )
        connection.execute(
            text(
                """
                update outbox
                set aggregate_type = 'forged',
                    aggregate_id = 'forged',
                    sequence = 99,
                    topic = 'forged.snapshot',
                    payload = '{}'::jsonb,
                    idempotency_key = 'forged'
                """
            )
        )

    retried = complete_snapshot(
        command,
        now=request.requested_at + timedelta(seconds=2),
        artifacts=artifacts,
        completions=store,
        policy=provider_policy(),
    )

    assert isinstance(retried, Failure)
    assert retried.error.code is ErrorCode.CONFLICT


@pytest.mark.parametrize(
    "tamper_sql",
    (
        "update job set payload = jsonb_set(payload, '{query,symbol}', '\"MSFT\"')",
        "update job set payload_hash = repeat('f', 64)",
        "update run set input_hash = repeat('e', 64)",
        "update run set as_of = as_of + interval '1 day'",
        "update run set policy_id = 'rogue-policy/1'",
        "update artifact_manifest set size_bytes = size_bytes + 1 where source = 'replay'",
        "update artifact_manifest set metadata = '{}'::jsonb where source = 'replay'",
        'update evidence_item set payload = \'{"close":"999.00"}\'::jsonb',
        "update evidence_item set available_at = available_at - interval '1 second'",
        "update evidence_item set content_hash = repeat('d', 64)",
        "update evidence_item set quality = '{}'::jsonb",
        "update dataset_snapshot set manifest = '{}'::jsonb",
        "delete from dataset_snapshot_evidence",
        "delete from run_dataset_snapshot",
    ),
)
def test_succeeded_retry_revalidates_entire_canonical_graph(
    clean_database: Engine,
    tamper_sql: str,
) -> None:
    request = snapshot_request(clean_database)
    PostgresSnapshotRequestStore(clean_database).submit(request)
    lease = unwrap(
        PostgresJobQueue(clean_database).claim(
            worker_id="worker-a",
            now=request.requested_at,
            lease_for=timedelta(minutes=5),
        )
    )
    artifacts = MemoryArtifactStore()
    snapshot = unwrap(materialize_snapshot(request, materialization(), artifacts))
    command = completion(lease, snapshot)
    store = PostgresSnapshotCompletionStore(clean_database)
    unwrap(
        complete_snapshot(
            command,
            now=request.requested_at + timedelta(seconds=1),
            artifacts=artifacts,
            completions=store,
            policy=provider_policy(),
        )
    )
    with clean_database.begin() as connection:
        connection.execute(text("set local session_replication_role = replica"))
        connection.execute(text(tamper_sql))

    retried = complete_snapshot(
        command,
        now=request.requested_at + timedelta(seconds=2),
        artifacts=artifacts,
        completions=store,
        policy=provider_policy(),
    )

    assert isinstance(retried, Failure)
    assert retried.error.code is ErrorCode.CONFLICT


def test_snapshot_for_different_request_is_rejected_before_any_insert(
    clean_database: Engine,
) -> None:
    request = snapshot_request(clean_database)
    PostgresSnapshotRequestStore(clean_database).submit(request)
    lease = unwrap(
        PostgresJobQueue(clean_database).claim(
            worker_id="worker-a",
            now=request.requested_at,
            lease_for=timedelta(minutes=5),
        )
    )
    other_request = snapshot_request(clean_database, query={"symbol": "MSFT"})
    artifacts = MemoryArtifactStore()
    snapshot = unwrap(materialize_snapshot(other_request, materialization(), artifacts))

    result = complete_snapshot(
        completion(lease, snapshot),
        now=request.requested_at + timedelta(seconds=1),
        artifacts=artifacts,
        completions=PostgresSnapshotCompletionStore(clean_database),
        policy=provider_policy(),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    assert table_counts(clean_database) == (0, 0, 0, 0, 0, 0, 0)


@pytest.mark.parametrize(
    ("provider", "endpoint"),
    (("rogue", "/v1/prices"), ("replay", "/rogue")),
)
def test_completion_transaction_rejects_unauthorized_provider_route(
    clean_database: Engine,
    provider: str,
    endpoint: str,
) -> None:
    request = snapshot_request(clean_database)
    PostgresSnapshotRequestStore(clean_database).submit(request)
    lease = unwrap(
        PostgresJobQueue(clean_database).claim(
            worker_id="worker-a",
            now=request.requested_at,
            lease_for=timedelta(minutes=5),
        )
    )
    artifacts = MemoryArtifactStore()
    snapshot = unwrap(
        materialize_snapshot(
            request,
            materialization(provider=provider, endpoint=endpoint),
            artifacts,
        )
    )

    result = complete_snapshot(
        completion(lease, snapshot),
        now=request.requested_at + timedelta(seconds=1),
        artifacts=artifacts,
        completions=PostgresSnapshotCompletionStore(clean_database),
        policy=provider_policy(),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    assert table_counts(clean_database) == (0, 0, 0, 0, 0, 0, 0)


def test_database_failure_rolls_back_artifacts_and_all_canonical_rows(
    clean_database: Engine,
) -> None:
    request = snapshot_request(clean_database)
    PostgresSnapshotRequestStore(clean_database).submit(request)
    lease = unwrap(
        PostgresJobQueue(clean_database).claim(
            worker_id="worker-a",
            now=request.requested_at,
            lease_for=timedelta(minutes=5),
        )
    )
    artifacts = MemoryArtifactStore()
    snapshot = unwrap(materialize_snapshot(request, materialization(), artifacts))
    with clean_database.begin() as connection:
        connection.execute(
            text(
                """
                alter table evidence_item
                add constraint test_force_snapshot_rollback check (false)
                """
            )
        )
    try:
        result = complete_snapshot(
            completion(lease, snapshot),
            now=request.requested_at + timedelta(seconds=1),
            artifacts=artifacts,
            completions=PostgresSnapshotCompletionStore(clean_database),
            policy=provider_policy(),
        )
    finally:
        with clean_database.begin() as connection:
            connection.execute(
                text(
                    """
                    alter table evidence_item
                    drop constraint test_force_snapshot_rollback
                    """
                )
            )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    assert table_counts(clean_database) == (0, 0, 0, 0, 0, 0, 0)


def snapshot_request(
    engine: Engine,
    *,
    query: dict[str, object] | None = None,
    idempotency_key: str = "snapshot-completion",
) -> CreateSnapshotRequest:
    requested_at = database_now(engine)
    return CreateSnapshotRequest(
        market="US",
        capability="prices",
        as_of=EVIDENCE_AS_OF,
        query=query or {"symbol": "AAPL"},
        provider_policy_id="us-prices/1",
        idempotency_key=idempotency_key,
        owner_subject="test-owner",
        requested_at=requested_at,
    )


def materialization(
    *,
    raw: bytes = b'{"symbol":"AAPL","close":"100.00"}',
    provider: str = "replay",
    endpoint: str = "/v1/prices",
) -> ProviderSnapshotMaterialization:
    timeline = EvidenceTimeline(
        event_time=EVIDENCE_AS_OF - timedelta(minutes=1),
        published_at=EVIDENCE_AS_OF - timedelta(seconds=30),
        available_at=EVIDENCE_AS_OF,
        observed_at=EVIDENCE_AS_OF,
        as_of=EVIDENCE_AS_OF,
        availability_certainty=AvailabilityCertainty.PROVEN,
    )
    return ProviderSnapshotMaterialization(
        provider=provider,
        provider_version="fixture/1",
        endpoint=endpoint,
        raw_payload=raw,
        raw_media_type="application/json",
        license_tag="CC0-1.0",
        redistribution_tag="synthetic-unrestricted",
        sensitivity=Sensitivity.PUBLIC,
        observation=ProviderObservation[object](
            state=ProviderDataState.AVAILABLE,
            data=({"close": "100.00"},),
            completeness=Decimal("1"),
            observed_at=EVIDENCE_AS_OF,
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


def completion(lease: JobLease, snapshot: MaterializedSnapshot) -> CompleteSnapshotJob:
    return CompleteSnapshotJob(
        job_id=lease.job_id,
        worker_id=lease.lease_owner,
        attempt_generation=lease.attempt_generation,
        attempt_nonce=lease.attempt_nonce,
        snapshot=snapshot,
    )


def _complete_new_snapshot_run(
    engine: Engine,
    request: CreateSnapshotRequest,
    artifacts: MemoryArtifactStore,
    store: PostgresSnapshotCompletionStore,
    *,
    completed_at: datetime,
) -> CompleteSnapshotJob:
    PostgresSnapshotRequestStore(engine).submit(request)
    lease = unwrap(
        PostgresJobQueue(engine).claim(
            worker_id=f"worker-{request.idempotency_key}",
            now=completed_at - timedelta(seconds=1),
            lease_for=timedelta(minutes=5),
        )
    )
    snapshot = unwrap(materialize_snapshot(request, materialization(), artifacts))
    command = completion(lease, snapshot)
    unwrap(
        complete_snapshot(
            command,
            now=completed_at,
            artifacts=artifacts,
            completions=store,
            policy=provider_policy(),
        )
    )
    return command


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


def database_now(engine: Engine) -> datetime:
    with engine.connect() as connection:
        value = connection.scalar(text("select clock_timestamp()"))
    assert isinstance(value, datetime)
    assert value.tzinfo is not None and value.utcoffset() is not None
    return value


def unwrap[T](result: Result[T]) -> T:
    assert isinstance(result, Success)
    return result.value


def table_counts(engine: Engine) -> tuple[int, ...]:
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


def canonical_commit_times(engine: Engine) -> tuple[datetime, ...]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                select evidence.created_at, snapshot.created_at,
                       snapshot_evidence.created_at, run_snapshot.created_at,
                       event.occurred_at, outbox.created_at, outbox.not_before,
                       job.updated_at, run.updated_at
                from evidence_item evidence
                join dataset_snapshot snapshot on true
                join dataset_snapshot_evidence snapshot_evidence on true
                join run_dataset_snapshot run_snapshot on true
                join run_event event on true
                join outbox on true
                join job on true
                join run on run.run_id = job.run_id
                """
            )
        ).one()
    return tuple(row)


def job_run_state(engine: Engine) -> tuple[object, ...]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                select job.status, job.result_artifact_hash, job.updated_at,
                       run.status, run.version, run.updated_at
                from job
                join run on run.run_id = job.run_id
                """
            )
        ).one()
    return tuple(row)
