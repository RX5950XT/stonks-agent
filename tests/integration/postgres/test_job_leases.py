from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Connection, Engine, text

from stonks_agent.adapters.postgres.job_queue import PostgresJobQueue
from stonks_agent.adapters.postgres.unit_of_work import PostgresUnitOfWork
from stonks_agent.domain.artifact import ArtifactMetadata
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.job import CompleteJob, EnqueueJob
from stonks_agent.domain.workflow import CreateWorkflowRun
from stonks_agent.ports.artifact_store import ArtifactManifest
from stonks_contracts.evidence import Sensitivity

AS_OF = datetime(2026, 1, 2, 21, tzinfo=UTC)
RUN_ID = UUID("40000000-0000-4000-8000-000000000001")
JOB_ID = UUID("40000000-0000-4000-8000-000000000002")
RESULT_HASH = "9" * 64
pytestmark = pytest.mark.postgres


def test_skip_locked_allows_only_one_claim_per_job(clean_database: Engine) -> None:
    now = database_now(clean_database)
    seed_run_and_artifact(clean_database, now)
    queue = PostgresJobQueue(clean_database)
    assert isinstance(queue.enqueue(enqueue(now=now)), Success)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(
                lambda worker: queue.claim(
                    worker_id=worker,
                    now=now,
                    lease_for=timedelta(seconds=30),
                ),
                ("worker-a", "worker-b"),
            )
        )

    assert sum(isinstance(result, Success) for result in results) == 1
    assert sum(isinstance(result, Failure) for result in results) == 1


def test_expired_attempt_is_fenced_and_reclaimed(clean_database: Engine) -> None:
    now = database_now(clean_database)
    seed_run_and_artifact(clean_database, now)
    queue = PostgresJobQueue(clean_database)
    queue.enqueue(enqueue(now=now))
    stale = unwrap(
        queue.claim(
            worker_id="worker-a",
            now=now,
            lease_for=timedelta(seconds=1),
        )
    )
    with clean_database.begin() as connection:
        connection.execute(
            text("update job set lease_until = :expired where job_id = :job_id"),
            {"expired": now - timedelta(seconds=1), "job_id": JOB_ID},
        )
    current = unwrap(
        queue.claim(
            worker_id="worker-b",
            now=now,
            lease_for=timedelta(seconds=30),
        )
    )

    rejected = queue.complete(
        CompleteJob(
            job_id=JOB_ID,
            worker_id="worker-a",
            attempt_generation=stale.attempt_generation,
            attempt_nonce=stale.attempt_nonce,
            result_artifact_hash=RESULT_HASH,
        ),
        now=now,
    )

    assert current.attempt_generation == stale.attempt_generation + 1
    assert current.attempt_nonce != stale.attempt_nonce
    assert isinstance(rejected, Failure)
    assert rejected.error.code is ErrorCode.CONFLICT
    assert table_count(clean_database, "run_event") == 0
    assert table_count(clean_database, "outbox") == 0


def test_crash_after_commit_retry_does_not_duplicate_event_or_outbox(
    clean_database: Engine,
) -> None:
    now = database_now(clean_database)
    seed_run_and_artifact(clean_database, now)
    queue = PostgresJobQueue(clean_database)
    queue.enqueue(enqueue(now=now))
    lease = unwrap(
        queue.claim(
            worker_id="worker-a",
            now=now,
            lease_for=timedelta(seconds=30),
        )
    )
    completion = CompleteJob(
        job_id=JOB_ID,
        worker_id="worker-a",
        attempt_generation=lease.attempt_generation,
        attempt_nonce=lease.attempt_nonce,
        result_artifact_hash=RESULT_HASH,
    )

    first = unwrap(queue.complete(completion, now=now))
    retried = unwrap(queue.complete(completion, now=now))

    assert retried == first
    assert table_count(clean_database, "run_event") == 1
    assert table_count(clean_database, "outbox") == 1


def test_completion_atomically_registers_worker_artifact_event_outbox_and_ack(
    clean_database: Engine,
) -> None:
    now = database_now(clean_database)
    seed_run(clean_database, now)
    queue = PostgresJobQueue(clean_database)
    unwrap(
        queue.enqueue(
            enqueue(now=now).model_copy(update={"job_type": "tradingagents_research"})
        )
    )
    lease = unwrap(
        queue.claim(
            worker_id="core-runner",
            now=now,
            lease_for=timedelta(seconds=30),
        )
    )
    manifest = ArtifactManifest(
        content_hash=RESULT_HASH,
        size_bytes=1,
        metadata=ArtifactMetadata(
            media_type="application/json",
            license_tag="Apache-2.0",
            sensitivity=Sensitivity.INTERNAL,
            source="tradingagents-isolated-worker",
            attributes=(("schema", "tradingagents-worker-result/1.0.0"),),
        ),
        finalized_at=now,
        storage_uri=f"artifact://sha256/{RESULT_HASH}",
    )

    completed = queue.complete(
        CompleteJob(
            job_id=JOB_ID,
            worker_id="core-runner",
            attempt_generation=lease.attempt_generation,
            attempt_nonce=lease.attempt_nonce,
            result_artifact_hash=RESULT_HASH,
        ),
        now=now,
        artifact=manifest,
    )

    assert isinstance(completed, Success)
    with clean_database.connect() as connection:
        state = connection.execute(
            text(
                "select j.status, j.result_artifact_hash, a.source, "
                "count(distinct e.event_id), count(distinct o.outbox_id) "
                "from job j join artifact_manifest a "
                "on a.content_hash = j.result_artifact_hash "
                "join run_event e using (run_id) "
                "join outbox o on o.aggregate_id = j.run_id::text "
                "where j.job_id = :job_id "
                "group by j.status, j.result_artifact_hash, a.source"
            ),
            {"job_id": JOB_ID},
        ).one()
    assert state == (
        "succeeded",
        RESULT_HASH,
        "tradingagents-isolated-worker",
        1,
        1,
    )


def test_idempotency_key_rejects_different_job_payload(clean_database: Engine) -> None:
    now = database_now(clean_database)
    seed_run_and_artifact(clean_database, now)
    queue = PostgresJobQueue(clean_database)

    first = queue.enqueue(enqueue(now=now, payload={"snapshot_id": "one"}))
    same = queue.enqueue(enqueue(now=now, payload={"snapshot_id": "one"}))
    conflict = queue.enqueue(enqueue(now=now, payload={"snapshot_id": "two"}))

    assert isinstance(first, Success)
    assert isinstance(same, Success)
    assert same.value == first.value
    assert isinstance(conflict, Failure)
    assert conflict.error.code is ErrorCode.CONFLICT


def test_deadline_and_attempt_limit_move_job_to_dead_letter(
    clean_database: Engine,
) -> None:
    now = database_now(clean_database)
    seed_run_and_artifact(clean_database, now)
    queue = PostgresJobQueue(clean_database)
    queue.enqueue(enqueue(now=now, max_attempts=1))
    unwrap(
        queue.claim(
            worker_id="worker-a",
            now=now,
            lease_for=timedelta(seconds=1),
        )
    )
    with clean_database.begin() as connection:
        connection.execute(
            text("update job set lease_until = :expired where job_id = :job_id"),
            {"expired": now - timedelta(seconds=1), "job_id": JOB_ID},
        )

    unavailable = queue.claim(
        worker_id="worker-b",
        now=now,
        lease_for=timedelta(seconds=30),
    )

    assert isinstance(unavailable, Failure)
    assert unavailable.error.code is ErrorCode.NOT_FOUND
    with clean_database.connect() as connection:
        status = connection.scalar(
            text("select status from job where job_id = :job_id"),
            {"job_id": JOB_ID},
        )
    assert status == "dead_letter"


def test_completion_requires_finalized_result_artifact(clean_database: Engine) -> None:
    now = database_now(clean_database)
    seed_run_and_artifact(clean_database, now)
    queue = PostgresJobQueue(clean_database)
    queue.enqueue(enqueue(now=now))
    lease = unwrap(
        queue.claim(
            worker_id="worker-a",
            now=now,
            lease_for=timedelta(seconds=30),
        )
    )

    result = queue.complete(
        CompleteJob(
            job_id=JOB_ID,
            worker_id="worker-a",
            attempt_generation=lease.attempt_generation,
            attempt_nonce=lease.attempt_nonce,
            result_artifact_hash="8" * 64,
        ),
        now=now,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.NOT_FOUND
    assert table_count(clean_database, "run_event") == 0


def test_not_before_and_deadline_are_enforced(clean_database: Engine) -> None:
    now = database_now(clean_database)
    seed_run_and_artifact(clean_database, now)
    queue = PostgresJobQueue(clean_database)
    queue.enqueue(
        enqueue(
            now=now,
            not_before=now + timedelta(minutes=5),
            deadline_at=now + timedelta(minutes=10),
        )
    )

    early = queue.claim(
        worker_id="worker-a",
        now=now,
        lease_for=timedelta(seconds=1),
    )
    with clean_database.begin() as connection:
        connection.execute(
            text("update job set not_before = :due where job_id = :job_id"),
            {"due": now - timedelta(seconds=1), "job_id": JOB_ID},
        )
    on_time = queue.claim(
        worker_id="worker-a",
        now=now,
        lease_for=timedelta(seconds=1),
    )

    assert isinstance(early, Failure)
    assert early.error.code is ErrorCode.NOT_FOUND
    assert isinstance(on_time, Success)


def test_future_caller_cannot_claim_job_before_database_not_before(
    clean_database: Engine,
) -> None:
    anchor = database_now(clean_database)
    seed_run_and_artifact(clean_database, anchor)
    queue = PostgresJobQueue(clean_database)
    unwrap(
        queue.enqueue(
            enqueue(
                now=anchor,
                not_before=anchor + timedelta(minutes=10),
                deadline_at=anchor + timedelta(minutes=20),
            )
        )
    )

    result = queue.claim(
        worker_id="future-caller",
        now=anchor + timedelta(minutes=15),
        lease_for=timedelta(minutes=1),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.NOT_FOUND


def test_stale_caller_cannot_complete_database_expired_job_lease(
    clean_database: Engine,
) -> None:
    anchor = database_now(clean_database)
    seed_run_and_artifact(clean_database, anchor)
    queue = PostgresJobQueue(clean_database)
    unwrap(
        queue.enqueue(
            enqueue(
                now=anchor,
                not_before=anchor - timedelta(minutes=1),
                deadline_at=anchor + timedelta(minutes=10),
            )
        )
    )
    lease = unwrap(
        queue.claim(
            worker_id="worker-a",
            now=anchor,
            lease_for=timedelta(minutes=5),
        )
    )
    with clean_database.begin() as connection:
        connection.execute(
            text("update job set lease_until = :expired where job_id = :job_id"),
            {"expired": anchor - timedelta(seconds=1), "job_id": JOB_ID},
        )

    result = queue.complete(
        CompleteJob(
            job_id=JOB_ID,
            worker_id="worker-a",
            attempt_generation=lease.attempt_generation,
            attempt_nonce=lease.attempt_nonce,
            result_artifact_hash=RESULT_HASH,
        ),
        now=anchor - timedelta(days=1),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    assert table_count(clean_database, "run_event") == 0


def test_database_expired_job_is_dead_lettered_despite_stale_caller(
    clean_database: Engine,
) -> None:
    anchor = database_now(clean_database)
    seed_run_and_artifact(clean_database, anchor)
    queue = PostgresJobQueue(clean_database)
    unwrap(
        queue.enqueue(
            enqueue(
                now=anchor,
                not_before=anchor - timedelta(minutes=10),
                deadline_at=anchor - timedelta(minutes=1),
            )
        )
    )

    result = queue.claim(
        worker_id="stale-caller",
        now=anchor - timedelta(minutes=5),
        lease_for=timedelta(minutes=1),
    )

    assert isinstance(result, Failure)
    with clean_database.connect() as connection:
        state = connection.execute(
            text(
                "select j.status, e.occurred_at from job j "
                "join run_event e using (run_id) where j.job_id = :job_id"
            ),
            {"job_id": JOB_ID},
        ).one()
    assert state[0] == "dead_letter"
    assert state[1] >= anchor


def test_claim_and_completion_commit_database_timestamps(
    clean_database: Engine,
) -> None:
    anchor = database_now(clean_database)
    seed_run_and_artifact(clean_database, anchor)
    queue = PostgresJobQueue(clean_database)
    unwrap(
        queue.enqueue(
            enqueue(
                now=anchor,
                not_before=anchor - timedelta(minutes=1),
                deadline_at=anchor + timedelta(minutes=10),
            )
        )
    )

    lease = unwrap(
        queue.claim(
            worker_id="future-caller",
            now=anchor + timedelta(days=1),
            lease_for=timedelta(minutes=5),
        )
    )
    receipt = unwrap(
        queue.complete(
            CompleteJob(
                job_id=JOB_ID,
                worker_id="future-caller",
                attempt_generation=lease.attempt_generation,
                attempt_nonce=lease.attempt_nonce,
                result_artifact_hash=RESULT_HASH,
            ),
            now=anchor + timedelta(days=1),
        )
    )

    assert anchor < lease.lease_until < anchor + timedelta(minutes=6)
    assert anchor < receipt.completed_at < anchor + timedelta(minutes=1)
    with clean_database.connect() as connection:
        timestamps = connection.execute(
            text(
                "select j.updated_at, r.updated_at, e.occurred_at, "
                "o.created_at, o.not_before from job j "
                "join run r using (run_id) join run_event e using (run_id) "
                "join outbox o on o.aggregate_id = r.run_id::text "
                "where j.job_id = :job_id"
            ),
            {"job_id": JOB_ID},
        ).one()
    assert all(value == receipt.completed_at for value in timestamps)


def enqueue(
    *,
    now: datetime,
    payload: dict[str, object] | None = None,
    max_attempts: int = 3,
    not_before: datetime | None = None,
    deadline_at: datetime | None = None,
) -> EnqueueJob:
    return EnqueueJob(
        job_id=JOB_ID,
        run_id=RUN_ID,
        job_type="research",
        payload=payload or {"snapshot_id": "one"},
        idempotency_key="job-idempotency",
        not_before=not_before or now,
        deadline_at=deadline_at or now + timedelta(minutes=5),
        max_attempts=max_attempts,
        created_at=now,
    )


def seed_run_and_artifact(engine: Engine, now: datetime) -> None:
    seed_run(engine, now)
    with engine.begin() as connection:
        insert_artifact(connection, now)


def seed_run(engine: Engine, now: datetime) -> None:
    with PostgresUnitOfWork(engine) as uow:
        created = uow.workflows.create(
            CreateWorkflowRun(
                run_id=RUN_ID,
                run_type="ingestion",
                as_of=AS_OF,
                policy_id="policy/1",
                idempotency_key="run-for-job",
                input_hash="7" * 64,
                created_at=now,
            )
        )
        assert isinstance(created, Success)
        uow.commit()


def insert_artifact(connection: Connection, now: datetime) -> None:
    connection.execute(
        text(
            """
            insert into artifact_manifest
                (content_hash, size_bytes, media_type, license_tag, sensitivity,
                 source, finalized_at, storage_uri, metadata)
            values
                (:hash, 1, 'application/json', 'test-only', 'internal',
                 'fixture', :now, :uri, '{}'::jsonb)
            """
        ),
        {
            "hash": RESULT_HASH,
            "now": now,
            "uri": f"artifact://sha256/{RESULT_HASH}",
        },
    )


def unwrap[T](result: object) -> T:
    assert isinstance(result, Success)
    return result.value  # type: ignore[return-value]


def table_count(engine: Engine, table: str) -> int:
    assert table in {"run_event", "outbox"}
    with engine.connect() as connection:
        return int(connection.scalar(text(f"select count(*) from {table}")) or 0)


def database_now(engine: Engine) -> datetime:
    with engine.connect() as connection:
        value = connection.scalar(text("select clock_timestamp()"))
    assert isinstance(value, datetime)
    return value
