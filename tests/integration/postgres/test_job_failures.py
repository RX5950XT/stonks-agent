from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine, text

from stonks_agent.adapters.postgres.job_queue import PostgresJobQueue
from stonks_agent.adapters.postgres.unit_of_work import PostgresUnitOfWork
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_agent.domain.job import EnqueueJob, FailJob, JobLease
from stonks_agent.domain.workflow import CreateWorkflowRun
from stonks_contracts.common import stable_payload_hash

pytestmark = pytest.mark.postgres
AS_OF = datetime(2026, 7, 28, 8, tzinfo=UTC)
RUN_ID = UUID("74000000-0000-4000-8000-000000000001")
JOB_ID = UUID("74000000-0000-4000-8000-000000000002")


def test_active_fence_atomically_dead_letters_job_and_audits_reason(
    clean_database: Engine,
) -> None:
    queue, lease, database_timestamp = leased_job(clean_database)
    request = failure_for(lease)

    result = queue.fail(
        request,
        now=database_timestamp - timedelta(days=365),
    )

    receipt = unwrap(result)
    assert receipt.job_id == JOB_ID
    assert receipt.run_id == RUN_ID
    assert receipt.sequence == 2
    assert receipt.error_code is ErrorCode.CAPABILITY_DENIED
    assert receipt.reason_code == "unknown_job_type"
    assert receipt.failed_at >= database_timestamp
    with clean_database.connect() as connection:
        state = connection.execute(
            text(
                """
                select j.status, j.lease_owner, j.attempt_nonce, j.lease_until,
                       j.last_error, j.updated_at, r.status, r.version,
                       e.event_type, e.payload, o.topic, o.payload,
                       o.idempotency_key
                from job j
                join run r using (run_id)
                join run_event e using (run_id)
                join outbox o on o.aggregate_id = r.run_id::text
                where j.job_id = :job_id
                """
            ),
            {"job_id": JOB_ID},
        ).one()
    expected_payload = {
        "job_id": str(JOB_ID),
        "job_type": "unknown",
        "attempt_generation": lease.attempt_generation,
        "worker_id": lease.lease_owner,
        "attempt_nonce_hash": stable_payload_hash(
            {"attempt_nonce": lease.attempt_nonce}
        ),
        "error_code": ErrorCode.CAPABILITY_DENIED.value,
        "reason": "unknown_job_type",
        "status": "dead_letter",
        "job_identity_hash": state[9]["job_identity_hash"],
        "run_identity_hash": state[9]["run_identity_hash"],
    }
    assert state[0:4] == ("dead_letter", None, None, None)
    assert state[4] == {
        "code": "capability_denied",
        "reason": "unknown_job_type",
        "attempt_generation": lease.attempt_generation,
    }
    assert state[5] == receipt.failed_at
    assert state[6:9] == ("failed", 2, "job.dead_lettered")
    assert state[9] == expected_payload
    assert state[10:12] == ("job.dead_lettered", expected_payload)
    assert state[12] == (f"job:{JOB_ID}:dead-letter:{lease.attempt_generation}")


def test_identical_failure_retry_returns_same_receipt_without_duplicate_audit(
    clean_database: Engine,
) -> None:
    queue, lease, now = leased_job(clean_database)
    request = failure_for(lease)

    first = unwrap(queue.fail(request, now=now))
    retried = unwrap(queue.fail(request, now=now + timedelta(days=365)))

    assert retried == first
    assert table_count(clean_database, "run_event") == 1
    assert table_count(clean_database, "outbox") == 1


@pytest.mark.parametrize(
    "tamper_sql",
    (
        "update job set last_error = jsonb_build_object("
        "'code', 'internal_error', 'reason', 'rogue', 'attempt_generation', 1)",
        "update run_event set event_hash = repeat('b', 64)",
        "update outbox set topic = 'rogue'",
    ),
)
def test_failure_retry_rejects_tampered_canonical_graph(
    clean_database: Engine,
    tamper_sql: str,
) -> None:
    queue, lease, now = leased_job(clean_database)
    request = failure_for(lease)
    unwrap(queue.fail(request, now=now))
    with clean_database.begin() as connection:
        connection.execute(text("set local session_replication_role = replica"))
        connection.execute(text(tamper_sql))

    result = queue.fail(request, now=now)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("worker_id", "worker-b"),
        ("attempt_generation", 2),
        ("attempt_nonce", "different-nonce"),
        ("error_code", ErrorCode.INTERNAL_ERROR),
        ("reason_code", "handler_failed"),
    ),
)
def test_failure_retry_rejects_a_different_command(
    clean_database: Engine,
    field: str,
    value: object,
) -> None:
    queue, lease, now = leased_job(clean_database)
    request = failure_for(lease)
    unwrap(queue.fail(request, now=now))

    result = queue.fail(
        request.model_copy(update={field: value}),
        now=now,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    assert table_count(clean_database, "run_event") == 1
    assert table_count(clean_database, "outbox") == 1


def test_expired_fence_cannot_be_failed_with_a_forged_caller_time(
    clean_database: Engine,
) -> None:
    queue, lease, now = leased_job(clean_database)
    with clean_database.begin() as connection:
        connection.execute(
            text("update job set lease_until = :expired where job_id = :job_id"),
            {"expired": now - timedelta(seconds=1), "job_id": JOB_ID},
        )

    result = queue.fail(
        failure_for(lease),
        now=now - timedelta(days=365),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    assert table_count(clean_database, "run_event") == 0
    assert table_count(clean_database, "outbox") == 0


def test_snapshot_failure_requires_its_canonical_completion_path(
    clean_database: Engine,
) -> None:
    queue, lease, now = leased_job(clean_database, job_type="create_snapshot")

    result = queue.fail(failure_for(lease), now=now)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CAPABILITY_DENIED
    assert table_count(clean_database, "run_event") == 0
    assert table_count(clean_database, "outbox") == 0
    with clean_database.connect() as connection:
        status = connection.scalar(
            text("select status from job where job_id = :job_id"),
            {"job_id": JOB_ID},
        )
    assert status == "leased"


def leased_job(
    engine: Engine,
    *,
    job_type: str = "unknown",
) -> tuple[PostgresJobQueue, JobLease, datetime]:
    now = database_now(engine)
    seed_run(engine, now)
    queue = PostgresJobQueue(engine)
    unwrap(queue.enqueue(enqueue(now=now, job_type=job_type)))
    lease = unwrap(
        queue.claim(
            worker_id="worker-a",
            now=now,
            lease_for=timedelta(seconds=30),
        )
    )
    return queue, lease, now


def enqueue(*, now: datetime, job_type: str) -> EnqueueJob:
    return EnqueueJob(
        job_id=JOB_ID,
        run_id=RUN_ID,
        job_type=job_type,
        payload={"task": "fail-closed"},
        idempotency_key="job-failure",
        not_before=now,
        deadline_at=now + timedelta(minutes=5),
        max_attempts=3,
        created_at=now,
    )


def failure_for(lease: JobLease) -> FailJob:
    return FailJob(
        job_id=lease.job_id,
        worker_id=lease.lease_owner,
        attempt_generation=lease.attempt_generation,
        attempt_nonce=lease.attempt_nonce,
        error_code=ErrorCode.CAPABILITY_DENIED,
        reason_code="unknown_job_type",
    )


def seed_run(engine: Engine, now: datetime) -> None:
    with PostgresUnitOfWork(engine) as uow:
        result = uow.workflows.create(
            CreateWorkflowRun(
                run_id=RUN_ID,
                run_type="worker-dispatch",
                as_of=AS_OF,
                policy_id="policy/1",
                idempotency_key="job-failure-run",
                input_hash="7" * 64,
                owner_subject="system:test",
                created_at=now,
            )
        )
        assert isinstance(result, Success)
        uow.commit()


def database_now(engine: Engine) -> datetime:
    with engine.connect() as connection:
        value = connection.scalar(text("select clock_timestamp()"))
    assert isinstance(value, datetime)
    return value


def table_count(engine: Engine, table: str) -> int:
    assert table in {"run_event", "outbox"}
    with engine.connect() as connection:
        value = connection.scalar(text(f"select count(*) from {table}"))
    assert isinstance(value, int)
    return value


def unwrap[T](result: Result[T]) -> T:
    assert isinstance(result, Success)
    return result.value
