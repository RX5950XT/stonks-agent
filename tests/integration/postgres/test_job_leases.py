from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Connection, Engine, text

from stonks_agent.adapters.postgres.job_queue import PostgresJobQueue
from stonks_agent.adapters.postgres.unit_of_work import PostgresUnitOfWork
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.job import CompleteJob, EnqueueJob
from stonks_agent.domain.workflow import CreateWorkflowRun

NOW = datetime(2026, 1, 2, 21, tzinfo=UTC)
RUN_ID = UUID("40000000-0000-4000-8000-000000000001")
JOB_ID = UUID("40000000-0000-4000-8000-000000000002")
RESULT_HASH = "9" * 64
pytestmark = pytest.mark.postgres


def test_skip_locked_allows_only_one_claim_per_job(clean_database: Engine) -> None:
    seed_run_and_artifact(clean_database)
    queue = PostgresJobQueue(clean_database)
    assert isinstance(queue.enqueue(enqueue()), Success)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(
                lambda worker: queue.claim(
                    worker_id=worker,
                    now=NOW,
                    lease_for=timedelta(seconds=30),
                ),
                ("worker-a", "worker-b"),
            )
        )

    assert sum(isinstance(result, Success) for result in results) == 1
    assert sum(isinstance(result, Failure) for result in results) == 1


def test_expired_attempt_is_fenced_and_reclaimed(clean_database: Engine) -> None:
    seed_run_and_artifact(clean_database)
    queue = PostgresJobQueue(clean_database)
    queue.enqueue(enqueue())
    stale = unwrap(
        queue.claim(
            worker_id="worker-a",
            now=NOW,
            lease_for=timedelta(seconds=1),
        )
    )
    current = unwrap(
        queue.claim(
            worker_id="worker-b",
            now=NOW + timedelta(seconds=2),
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
        now=NOW + timedelta(seconds=3),
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
    seed_run_and_artifact(clean_database)
    queue = PostgresJobQueue(clean_database)
    queue.enqueue(enqueue())
    lease = unwrap(
        queue.claim(
            worker_id="worker-a",
            now=NOW,
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

    first = unwrap(queue.complete(completion, now=NOW + timedelta(seconds=1)))
    retried = unwrap(queue.complete(completion, now=NOW + timedelta(seconds=2)))

    assert retried == first
    assert table_count(clean_database, "run_event") == 1
    assert table_count(clean_database, "outbox") == 1


def test_idempotency_key_rejects_different_job_payload(clean_database: Engine) -> None:
    seed_run_and_artifact(clean_database)
    queue = PostgresJobQueue(clean_database)

    first = queue.enqueue(enqueue(payload={"snapshot_id": "one"}))
    same = queue.enqueue(enqueue(payload={"snapshot_id": "one"}))
    conflict = queue.enqueue(enqueue(payload={"snapshot_id": "two"}))

    assert isinstance(first, Success)
    assert isinstance(same, Success)
    assert same.value == first.value
    assert isinstance(conflict, Failure)
    assert conflict.error.code is ErrorCode.CONFLICT


def test_deadline_and_attempt_limit_move_job_to_dead_letter(
    clean_database: Engine,
) -> None:
    seed_run_and_artifact(clean_database)
    queue = PostgresJobQueue(clean_database)
    queue.enqueue(enqueue(max_attempts=1))
    unwrap(
        queue.claim(
            worker_id="worker-a",
            now=NOW,
            lease_for=timedelta(seconds=1),
        )
    )

    unavailable = queue.claim(
        worker_id="worker-b",
        now=NOW + timedelta(seconds=2),
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
    seed_run_and_artifact(clean_database)
    queue = PostgresJobQueue(clean_database)
    queue.enqueue(enqueue())
    lease = unwrap(
        queue.claim(
            worker_id="worker-a",
            now=NOW,
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
        now=NOW + timedelta(seconds=1),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.NOT_FOUND
    assert table_count(clean_database, "run_event") == 0


def test_not_before_and_deadline_are_enforced(clean_database: Engine) -> None:
    seed_run_and_artifact(clean_database)
    queue = PostgresJobQueue(clean_database)
    queue.enqueue(
        enqueue(
            not_before=NOW + timedelta(seconds=5),
            deadline_at=NOW + timedelta(seconds=10),
        )
    )

    early = queue.claim(
        worker_id="worker-a",
        now=NOW,
        lease_for=timedelta(seconds=1),
    )
    on_time = queue.claim(
        worker_id="worker-a",
        now=NOW + timedelta(seconds=5),
        lease_for=timedelta(seconds=1),
    )

    assert isinstance(early, Failure)
    assert early.error.code is ErrorCode.NOT_FOUND
    assert isinstance(on_time, Success)


def enqueue(
    *,
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
        not_before=not_before or NOW,
        deadline_at=deadline_at or NOW + timedelta(minutes=5),
        max_attempts=max_attempts,
        created_at=NOW,
    )


def seed_run_and_artifact(engine: Engine) -> None:
    with PostgresUnitOfWork(engine) as uow:
        created = uow.workflows.create(
            CreateWorkflowRun(
                run_id=RUN_ID,
                run_type="ingestion",
                as_of=NOW,
                policy_id="policy/1",
                idempotency_key="run-for-job",
                input_hash="7" * 64,
                created_at=NOW,
            )
        )
        assert isinstance(created, Success)
        uow.commit()
    with engine.begin() as connection:
        insert_artifact(connection)


def insert_artifact(connection: Connection) -> None:
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
            "now": NOW,
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
