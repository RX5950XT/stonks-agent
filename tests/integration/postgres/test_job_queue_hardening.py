from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine, text

from stonks_agent.adapters.postgres.job_queue import PostgresJobQueue
from stonks_agent.adapters.postgres.unit_of_work import PostgresUnitOfWork
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_agent.domain.job import CompleteJob, EnqueueJob, JobLease
from stonks_agent.domain.workflow import CreateWorkflowRun
from stonks_contracts.common import stable_payload_hash

pytestmark = pytest.mark.postgres
AS_OF = datetime(2026, 1, 2, 21, tzinfo=UTC)
RUN_ID = UUID("41000000-0000-4000-8000-000000000001")
OTHER_RUN_ID = UUID("41000000-0000-4000-8000-000000000002")
JOB_ID = UUID("41000000-0000-4000-8000-000000000003")
SECOND_JOB_ID = UUID("41000000-0000-4000-8000-000000000004")
RESULT_HASH = "a" * 64


def test_exhausted_lease_is_atomically_dead_lettered_and_audited(
    clean_database: Engine,
) -> None:
    now = database_now(clean_database)
    seed_run(clean_database, now=now)
    queue = PostgresJobQueue(clean_database)
    unwrap(queue.enqueue(job(now=now, max_attempts=1)))
    lease = unwrap(
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
    state = terminal_state(clean_database)
    assert state[0:7] == (
        "dead_letter",
        None,
        None,
        None,
        {"code": "attempts_exhausted", "attempt_generation": 1},
        "failed",
        2,
    )
    assert state[7] >= now
    assert state[8:10] == ("job.dead_lettered", "job.dead_lettered")
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
    assert all(value == state[7] for value in timestamps)
    assert_dead_letter_graph(clean_database, lease, "attempts_exhausted")


def test_expired_queued_job_is_terminally_audited(clean_database: Engine) -> None:
    now = database_now(clean_database)
    seed_run(clean_database, now=now)
    queue = PostgresJobQueue(clean_database)
    deadline = now - timedelta(minutes=1)
    unwrap(
        queue.enqueue(
            job(
                now=now,
                not_before=now - timedelta(minutes=2),
                deadline_at=deadline,
                created_at=now - timedelta(minutes=2),
            )
        )
    )

    result = queue.claim(
        worker_id="worker-a",
        now=now - timedelta(minutes=5),
        lease_for=timedelta(seconds=30),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.NOT_FOUND
    state = terminal_state(clean_database)
    assert state[0] == "dead_letter"
    assert state[4] == {"code": "deadline_exceeded", "attempt_generation": 0}
    assert state[5:7] == ("failed", 2)
    assert_dead_letter_graph(clean_database, None, "deadline_exceeded")


def test_concurrent_sweepers_commit_one_terminal_transition(
    clean_database: Engine,
) -> None:
    now = database_now(clean_database)
    seed_run(clean_database, now=now)
    queue = PostgresJobQueue(clean_database)
    unwrap(queue.enqueue(job(now=now, max_attempts=1)))
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

    def sweep(worker: str) -> Result[JobLease]:
        return PostgresJobQueue(clean_database).claim(
            worker_id=worker,
            now=now,
            lease_for=timedelta(seconds=30),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(sweep, ("worker-b", "worker-c")))

    assert all(isinstance(result, Failure) for result in results)
    with clean_database.connect() as connection:
        counts = connection.execute(
            text(
                "select (select count(*) from run_event), "
                "(select count(*) from outbox), "
                "(select version from run where run_id = :run_id)"
            ),
            {"run_id": RUN_ID},
        ).one()
    assert counts == (1, 1, 2)


@pytest.mark.parametrize(
    ("tamper_sql", "params"),
    (
        (
            'update job set payload = \'{"task": "rogue"}\'::jsonb, '
            "payload_hash = :payload_hash",
            {"payload_hash": stable_payload_hash({"task": "rogue"})},
        ),
        ("update job set payload_hash = repeat('b', 64)", {}),
        ("update job set job_type = 'rogue'", {}),
        ("update job set deadline_at = deadline_at + interval '1 second'", {}),
        ("update run set policy_id = 'rogue-policy/1'", {}),
        ("update run set version = version + 1", {}),
        ("update artifact_manifest set source = 'rogue'", {}),
        (
            "update run_event set payload = jsonb_set(payload, "
            "'{job_type}', '\"rogue\"')",
            {},
        ),
        ("update run_event set event_hash = repeat('c', 64)", {}),
        ("delete from run_event", {}),
        ("update outbox set topic = 'rogue'", {}),
        (
            "update outbox set payload = jsonb_set(payload, '{job_type}', '\"rogue\"')",
            {},
        ),
        ("update outbox set aggregate_id = 'rogue'", {}),
        ("update outbox set idempotency_key = 'rogue'", {}),
        ("delete from outbox", {}),
    ),
)
def test_completed_retry_fails_closed_on_tampered_canonical_graph(
    clean_database: Engine,
    tamper_sql: str,
    params: dict[str, object],
) -> None:
    queue, completion = completed_job(clean_database)
    with clean_database.begin() as connection:
        connection.execute(text("set local session_replication_role = replica"))
        connection.execute(text(tamper_sql), params)

    result = queue.complete(completion, now=database_now(clean_database))

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("job_id", UUID("41000000-0000-4000-8000-000000000099")),
        ("run_id", OTHER_RUN_ID),
        ("job_type", "other-research"),
        ("payload", {"task": "other"}),
        ("not_before", AS_OF + timedelta(seconds=1)),
        ("deadline_at", AS_OF + timedelta(minutes=6)),
        ("max_attempts", 4),
        ("created_at", AS_OF + timedelta(seconds=1)),
    ),
)
def test_enqueue_idempotency_compares_full_immutable_command(
    clean_database: Engine,
    field: str,
    value: object,
) -> None:
    now = database_now(clean_database)
    seed_run(clean_database, now=now)
    seed_run(clean_database, now=now, run_id=OTHER_RUN_ID, key="other-run")
    queue = PostgresJobQueue(clean_database)
    original = job(now=now)
    unwrap(queue.enqueue(original))
    changed = original.model_copy(update={field: value})

    result = queue.enqueue(changed)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT


@pytest.mark.parametrize(
    "tamper_sql",
    (
        'update job set payload = \'{"task": "rogue"}\'::jsonb',
        'update job set payload = \'{"task": "rogue"}\'::jsonb, '
        "payload_hash = repeat('d', 64)",
        "update job set job_type = 'rogue'",
        "update job set run_id = '41000000-0000-4000-8000-000000000002'",
        "update job set deadline_at = deadline_at + interval '1 second'",
        "update job set max_attempts = max_attempts + 1",
    ),
)
def test_enqueue_retry_rejects_tampered_stored_command(
    clean_database: Engine,
    tamper_sql: str,
) -> None:
    now = database_now(clean_database)
    seed_run(clean_database, now=now)
    seed_run(clean_database, now=now, run_id=OTHER_RUN_ID, key="other-run")
    queue = PostgresJobQueue(clean_database)
    original = job(now=now)
    unwrap(queue.enqueue(original))
    with clean_database.begin() as connection:
        connection.execute(text(tamper_sql))

    result = queue.enqueue(original)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT


def test_completed_retry_remains_valid_after_a_later_run_event(
    clean_database: Engine,
) -> None:
    now = database_now(clean_database)
    seed_run(clean_database, now=now)
    insert_artifact(clean_database, now=now)
    queue = PostgresJobQueue(clean_database)
    first_command = job(now=now)
    unwrap(queue.enqueue(first_command))
    first_lease = unwrap(
        queue.claim(
            worker_id="worker-a",
            now=now,
            lease_for=timedelta(seconds=30),
        )
    )
    first_completion = CompleteJob(
        job_id=first_command.job_id,
        worker_id="worker-a",
        attempt_generation=first_lease.attempt_generation,
        attempt_nonce=first_lease.attempt_nonce,
        result_artifact_hash=RESULT_HASH,
    )
    first_receipt = unwrap(queue.complete(first_completion, now=now))
    second_command = job(
        now=now,
        job_id=SECOND_JOB_ID,
        payload={"task": "summarize"},
        idempotency_key="job-hardening-second",
        not_before=now,
        created_at=now,
    )
    unwrap(queue.enqueue(second_command))
    second_lease = unwrap(
        queue.claim(
            worker_id="worker-b",
            now=now,
            lease_for=timedelta(seconds=30),
        )
    )
    unwrap(
        queue.complete(
            CompleteJob(
                job_id=second_command.job_id,
                worker_id="worker-b",
                attempt_generation=second_lease.attempt_generation,
                attempt_nonce=second_lease.attempt_nonce,
                result_artifact_hash=RESULT_HASH,
            ),
            now=now,
        )
    )

    retried = queue.complete(first_completion, now=now)

    assert isinstance(retried, Success)
    assert retried.value == first_receipt


def completed_job(engine: Engine) -> tuple[PostgresJobQueue, CompleteJob]:
    now = database_now(engine)
    seed_run(engine, now=now)
    insert_artifact(engine, now=now)
    queue = PostgresJobQueue(engine)
    unwrap(queue.enqueue(job(now=now)))
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
    unwrap(queue.complete(completion, now=now))
    return queue, completion


def job(*, now: datetime, **updates: object) -> EnqueueJob:
    command = EnqueueJob(
        job_id=JOB_ID,
        run_id=RUN_ID,
        job_type="research",
        payload={"task": "analyze"},
        idempotency_key="job-hardening",
        not_before=now,
        deadline_at=now + timedelta(minutes=5),
        max_attempts=3,
        created_at=now,
    )
    return command.model_copy(update=updates)


def seed_run(
    engine: Engine,
    *,
    now: datetime,
    run_id: UUID = RUN_ID,
    key: str = "job-hardening-run",
) -> None:
    with PostgresUnitOfWork(engine) as uow:
        result = uow.workflows.create(
            CreateWorkflowRun(
                run_id=run_id,
                run_type="ingestion",
                as_of=AS_OF,
                policy_id="policy/1",
                idempotency_key=key,
                input_hash="7" * 64,
                created_at=now,
            )
        )
        assert isinstance(result, Success)
        uow.commit()


def insert_artifact(engine: Engine, *, now: datetime) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                insert into artifact_manifest
                    (content_hash, size_bytes, media_type, license_tag, sensitivity,
                     source, finalized_at, storage_uri, metadata)
                values
                    (:hash, 1, 'application/json', 'test-only', 'internal',
                     'fixture', :now, :uri, '{"attributes": []}'::jsonb)
                """
            ),
            {
                "hash": RESULT_HASH,
                "now": now,
                "uri": f"artifact://sha256/{RESULT_HASH}",
            },
        )


def terminal_state(engine: Engine) -> tuple[object, ...]:
    with engine.connect() as connection:
        return connection.execute(
            text(
                """
                select j.status, j.lease_owner, j.attempt_nonce, j.lease_until,
                       j.last_error, r.status, r.version, r.updated_at,
                       e.event_type, o.topic
                from job j
                join run r on r.run_id = j.run_id
                join run_event e on e.run_id = r.run_id
                join outbox o on o.aggregate_id = r.run_id::text
                where j.job_id = :job_id
                """
            ),
            {"job_id": JOB_ID},
        ).one()


def assert_dead_letter_graph(
    engine: Engine,
    lease: JobLease | None,
    reason: str,
) -> None:
    generation = 0 if lease is None else lease.attempt_generation
    with engine.connect() as connection:
        event = (
            connection.execute(
                text("select * from run_event where run_id = :run_id"),
                {"run_id": RUN_ID},
            )
            .mappings()
            .one()
        )
        outbox = (
            connection.execute(
                text("select * from outbox where aggregate_id = :run_id"),
                {"run_id": str(RUN_ID)},
            )
            .mappings()
            .one()
        )
    expected_payload = {
        "job_id": str(JOB_ID),
        "job_type": "research",
        "attempt_generation": generation,
        "reason": reason,
        "status": "dead_letter",
    }
    assert event["payload"] == expected_payload
    assert event["previous_hash"] is None
    assert event["event_hash"] == stable_payload_hash(
        {
            "event_id": str(event["event_id"]),
            "sequence": event["sequence"],
            "previous_hash": None,
            "payload": expected_payload,
        }
    )
    assert outbox["payload"] == expected_payload
    assert outbox["sequence"] == event["sequence"]
    assert outbox["idempotency_key"] == (f"job:{JOB_ID}:dead-letter:{generation}")


def unwrap[T](result: Result[T]) -> T:
    assert isinstance(result, Success)
    return result.value


def database_now(engine: Engine) -> datetime:
    with engine.connect() as connection:
        value = connection.scalar(text("select clock_timestamp()"))
    assert isinstance(value, datetime)
    return value
