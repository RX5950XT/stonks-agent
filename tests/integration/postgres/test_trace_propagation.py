from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, inspect, text
from sqlalchemy.exc import IntegrityError

from stonks_agent.adapters.observability.context import (
    create_trace_context,
    trace_scope,
)
from stonks_agent.adapters.postgres.inbox import PostgresInbox
from stonks_agent.adapters.postgres.job_queue import PostgresJobQueue
from stonks_agent.adapters.postgres.outbox import PostgresOutbox
from stonks_agent.adapters.postgres.snapshot_requests import (
    PostgresSnapshotRequestStore,
)
from stonks_agent.adapters.postgres.unit_of_work import PostgresUnitOfWork
from stonks_agent.domain.errors import Result, Success
from stonks_agent.domain.inbox import InboxMessage
from stonks_agent.domain.job import CompleteJob, EnqueueJob
from stonks_agent.domain.snapshot import CreateSnapshotRequest
from stonks_agent.domain.telemetry import (
    ComponentName,
    CorrelationContext,
    OperationName,
    TraceCarrier,
    TraceContext,
)
from stonks_agent.domain.workflow import CreateWorkflowRun
from stonks_agent.ports.telemetry import OperationRecorderPort

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 7, 17, 8, tzinfo=UTC)
RUN_ID = UUID("b0000000-0000-4000-8000-000000000001")
JOB_ID = UUID("b0000000-0000-4000-8000-000000000002")
OUTBOX_ID = UUID("b0000000-0000-4000-8000-000000000003")
RESULT_HASH = "b" * 64
TRACE = TraceCarrier(
    traceparent="00-11111111111111111111111111111111-2222222222222222-01",
    tracestate="vendor=value",
)
CORRELATION_ID = "request-durable-1"


class RecordingOperationRecorder:
    def __init__(self, *, explode_after_call: bool = False) -> None:
        self.operations: list[tuple[ComponentName, OperationName]] = []
        self.explode_after_call = explode_after_call

    def record_result[T](
        self,
        *,
        component: ComponentName,
        operation: OperationName,
        call: Callable[[], Result[T]],
        parent: TraceContext | None = None,
    ) -> Result[T]:
        del parent
        result = call()
        self.operations.append((component, operation))
        if self.explode_after_call:
            raise RuntimeError("telemetry exporter failed")
        return result

    async def record_async_result[T](
        self,
        *,
        component: ComponentName,
        operation: OperationName,
        call: Callable[[], Awaitable[Result[T]]],
        parent: TraceContext | None = None,
    ) -> Result[T]:
        del component, operation, parent
        return await call()


class FixedGenerator:
    def new_trace_id(self) -> str:
        return "a" * 32

    def new_span_id(self) -> str:
        return "b" * 16


def test_job_enqueue_claim_and_completion_preserve_trace_without_changing_fence(
    clean_database: Engine,
) -> None:
    now = _database_now(clean_database)
    _seed_run_and_artifact(clean_database, now)
    queue = PostgresJobQueue(clean_database)

    record = _unwrap(queue.enqueue(_job(now)))
    lease = _unwrap(
        queue.claim(
            worker_id="worker-a",
            now=now,
            lease_for=timedelta(seconds=30),
        )
    )
    completion = _unwrap(
        queue.complete(
            CompleteJob(
                job_id=JOB_ID,
                worker_id="worker-a",
                attempt_generation=lease.attempt_generation,
                attempt_nonce=lease.attempt_nonce,
                result_artifact_hash=RESULT_HASH,
            ),
            now=now,
        )
    )

    assert record.trace_carrier == TRACE
    assert lease.trace_carrier == TRACE
    assert lease.correlation_id == CORRELATION_ID
    assert lease.attempt_generation == 1
    assert lease.attempt_nonce
    with clean_database.connect() as connection:
        stored = connection.execute(
            text(
                "select traceparent, tracestate, correlation_id "
                "from outbox where outbox_id = :outbox_id"
            ),
            {"outbox_id": completion.outbox_id},
        ).one()
    assert stored == (TRACE.traceparent, TRACE.tracestate, CORRELATION_ID)


def test_job_queue_records_enqueue_claim_and_complete_outside_transactions(
    clean_database: Engine,
) -> None:
    now = _database_now(clean_database)
    _seed_run_and_artifact(clean_database, now)
    recorder = RecordingOperationRecorder()
    assert isinstance(recorder, OperationRecorderPort)
    queue = PostgresJobQueue(clean_database, recorder=recorder)

    _unwrap(queue.enqueue(_job(now)))
    lease = _unwrap(
        queue.claim(
            worker_id="worker-a",
            now=now,
            lease_for=timedelta(seconds=30),
        )
    )
    _unwrap(
        queue.complete(
            CompleteJob(
                job_id=JOB_ID,
                worker_id="worker-a",
                attempt_generation=lease.attempt_generation,
                attempt_nonce=lease.attempt_nonce,
                result_artifact_hash=RESULT_HASH,
            ),
            now=now,
        )
    )

    assert recorder.operations == [
        (ComponentName.QUEUE, OperationName.ENQUEUE),
        (ComponentName.QUEUE, OperationName.CLAIM),
        (ComponentName.QUEUE, OperationName.COMPLETE),
    ]


def test_job_queue_recorder_failure_cannot_change_committed_result(
    clean_database: Engine,
) -> None:
    now = _database_now(clean_database)
    _seed_run(clean_database, now)
    recorder = RecordingOperationRecorder(explode_after_call=True)
    queue = PostgresJobQueue(clean_database, recorder=recorder)

    result = queue.enqueue(_job(now))

    assert isinstance(result, Success)
    with clean_database.connect() as connection:
        assert connection.scalar(text("select count(*) from job")) == 1


def test_job_idempotency_retry_keeps_original_trace_context(
    clean_database: Engine,
) -> None:
    now = _database_now(clean_database)
    _seed_run(clean_database, now)
    queue = PostgresJobQueue(clean_database)
    command = _job(now)

    first = _unwrap(queue.enqueue(command))
    retry = _unwrap(
        queue.enqueue(
            command.model_copy(
                update={
                    "trace_carrier": TraceCarrier(
                        traceparent=(
                            "00-33333333333333333333333333333333-4444444444444444-00"
                        )
                    ),
                    "correlation_id": "retry-request",
                }
            )
        )
    )

    assert retry == first
    assert retry.trace_carrier == TRACE
    assert retry.correlation_id == CORRELATION_ID


def test_api_originated_snapshot_job_captures_current_request_context(
    clean_database: Engine,
) -> None:
    context = create_trace_context(
        parent=TRACE,
        correlation=CorrelationContext(request_id=CORRELATION_ID),
        generator=FixedGenerator(),
    )
    request = CreateSnapshotRequest(
        market="US",
        capability="prices",
        as_of=NOW,
        query={"symbol": "AAPL"},
        provider_policy_id="us-prices/1",
        idempotency_key="snapshot-trace-1",
        owner_subject="researcher:test",
        requested_at=NOW,
    )

    with trace_scope(context):
        result = PostgresSnapshotRequestStore(clean_database).submit(request)

    assert isinstance(result, Success)
    with clean_database.connect() as connection:
        stored = connection.execute(
            text(
                "select traceparent, tracestate, correlation_id "
                "from job where job_id = :job_id"
            ),
            {"job_id": result.value.job_id},
        ).one()
    carrier = context.to_carrier()
    assert stored == (
        carrier.traceparent,
        carrier.tracestate,
        CORRELATION_ID,
    )


def test_outbox_claim_reconstructs_nullable_trace_context(
    clean_database: Engine,
) -> None:
    now = _database_now(clean_database)
    with clean_database.begin() as connection:
        _insert_outbox(connection, now)

    lease = _unwrap(
        PostgresOutbox(clean_database).claim(
            worker_id="publisher-a",
            now=now,
            lease_for=timedelta(seconds=30),
            limit=1,
        )
    )[0]

    assert lease.trace_carrier == TRACE
    assert lease.correlation_id == CORRELATION_ID


def test_inbox_duplicate_returns_original_trace_context(
    clean_database: Engine,
) -> None:
    inbox = PostgresInbox(clean_database)
    original = InboxMessage(
        consumer="consumer-a",
        message_id="message-1",
        payload={"status": "accepted"},
        received_at=NOW,
        processed_at=NOW,
        trace_carrier=TRACE,
        correlation_id=CORRELATION_ID,
    )
    retry = original.model_copy(
        update={"trace_carrier": None, "correlation_id": "retry-request"}
    )

    first = _unwrap(inbox.consume(original, lambda _: {"stored": True}))
    duplicate = _unwrap(inbox.consume(retry, lambda _: {"must_not_run": True}))

    assert first.trace_carrier == TRACE
    assert duplicate.duplicate
    assert duplicate.trace_carrier == TRACE
    assert duplicate.correlation_id == CORRELATION_ID


def test_migration_has_independent_bounded_trace_columns_and_constraints(
    migrated_engine: Engine,
) -> None:
    inspector = inspect(migrated_engine)
    for table in ("job", "outbox", "inbox"):
        columns = {column["name"]: column for column in inspector.get_columns(table)}
        assert columns["traceparent"]["type"].length == 55
        assert columns["tracestate"]["type"].length == 512
        assert columns["correlation_id"]["type"].length == 128
        assert all(
            columns[name]["nullable"]
            for name in (
                "traceparent",
                "tracestate",
                "correlation_id",
            )
        )
        constraints = {
            constraint["name"] for constraint in inspector.get_check_constraints(table)
        }
        assert {
            f"{table}_traceparent_valid",
            f"{table}_tracestate_valid",
            f"{table}_correlation_id_valid",
        } <= constraints


@pytest.mark.parametrize(
    ("table", "statement"),
    (
        (
            "job",
            "update job set traceparent = :invalid where job_id = :identity",
        ),
        (
            "outbox",
            "update outbox set traceparent = :invalid where outbox_id = :identity",
        ),
        (
            "inbox",
            """
            insert into inbox
                (consumer, message_id, payload_hash, received_at, processed_at,
                 result, traceparent)
            values
                ('invalid-consumer', 'message-2', :payload_hash, :now, :now,
                 '{}'::jsonb, :invalid)
            """,
        ),
    ),
)
def test_database_rejects_invalid_traceparent_on_every_transport_table(
    clean_database: Engine,
    table: str,
    statement: str,
) -> None:
    identities = _seed_transport_rows(clean_database)

    with pytest.raises(IntegrityError), clean_database.begin() as connection:
        connection.execute(
            text(statement),
            {
                "invalid": ("00-00000000000000000000000000000000-2222222222222222-01"),
                "identity": identities[table],
                "payload_hash": "a" * 64,
                "now": NOW,
            },
        )


def test_database_rejects_orphan_tracestate_and_invalid_correlation(
    clean_database: Engine,
) -> None:
    _seed_transport_rows(clean_database)

    with pytest.raises(IntegrityError), clean_database.begin() as connection:
        connection.execute(
            text(
                "update outbox set traceparent = null, tracestate = 'vendor=value' "
                "where outbox_id = :outbox_id"
            ),
            {"outbox_id": OUTBOX_ID},
        )
    with pytest.raises(IntegrityError), clean_database.begin() as connection:
        connection.execute(
            text(
                "update job set correlation_id = 'contains space' "
                "where job_id = :job_id"
            ),
            {"job_id": JOB_ID},
        )


def test_trace_columns_have_insert_but_no_update_grants(
    migrated_engine: Engine,
) -> None:
    with migrated_engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                select table_name, grantee, column_name, privilege_type
                from information_schema.column_privileges
                where table_schema = 'public'
                  and table_name in ('job', 'outbox', 'inbox')
                  and column_name in (
                      'traceparent', 'tracestate', 'correlation_id'
                  )
                  and grantee in ('stonks_app', 'stonks_worker')
                """
            )
        ).all()

    grants = {
        (row.table_name, row.grantee, row.column_name, row.privilege_type)
        for row in rows
    }
    assert not {grant for grant in grants if grant[3] == "UPDATE"}
    assert {
        (table, "stonks_app", column, "INSERT")
        for table in ("job", "outbox", "inbox")
        for column in ("traceparent", "tracestate", "correlation_id")
    } <= grants
    assert {
        (table, "stonks_worker", column, "INSERT")
        for table in ("job", "outbox", "inbox")
        for column in ("traceparent", "tracestate", "correlation_id")
    } <= grants


def test_trace_migration_downgrades_and_reupgrades(
    migrated_engine: Engine,
    alembic_config: Config,
) -> None:
    command.downgrade(alembic_config, "0015")
    try:
        inspector = inspect(migrated_engine)
        for table in ("job", "outbox", "inbox"):
            columns = {column["name"] for column in inspector.get_columns(table)}
            assert (
                not {
                    "traceparent",
                    "tracestate",
                    "correlation_id",
                }
                & columns
            )
    finally:
        command.upgrade(alembic_config, "head")

    inspector = inspect(migrated_engine)
    for table in ("job", "outbox", "inbox"):
        columns = {column["name"] for column in inspector.get_columns(table)}
        assert {
            "traceparent",
            "tracestate",
            "correlation_id",
        } <= columns


def _job(now: datetime) -> EnqueueJob:
    return EnqueueJob(
        job_id=JOB_ID,
        run_id=RUN_ID,
        job_type="research",
        payload={"snapshot_id": "snapshot-1"},
        idempotency_key="job-trace-1",
        not_before=now,
        deadline_at=now + timedelta(minutes=5),
        max_attempts=3,
        trace_carrier=TRACE,
        correlation_id=CORRELATION_ID,
        created_at=now,
    )


def _seed_run_and_artifact(engine: Engine, now: datetime) -> None:
    _seed_run(engine, now)
    with engine.begin() as connection:
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


def _seed_run(engine: Engine, now: datetime) -> None:
    with PostgresUnitOfWork(engine) as unit_of_work:
        result = unit_of_work.workflows.create(
            CreateWorkflowRun(
                run_id=RUN_ID,
                run_type="research",
                as_of=NOW,
                policy_id="policy/1",
                idempotency_key="trace-run-1",
                input_hash="7" * 64,
                owner_subject="system:test",
                created_at=now,
            )
        )
        assert isinstance(result, Success)
        unit_of_work.commit()


def _insert_outbox(connection: Connection, now: datetime) -> None:
    connection.execute(
        text(
            """
            insert into outbox
                (outbox_id, aggregate_type, aggregate_id, sequence, topic,
                 payload, idempotency_key, created_at, not_before, attempts,
                 traceparent, tracestate, correlation_id)
            values
                (:id, 'run', :aggregate_id, 1, 'test.event', '{}'::jsonb,
                 'trace-outbox-1', :now, :now, 0,
                 :traceparent, :tracestate, :correlation_id)
            """
        ),
        {
            "id": OUTBOX_ID,
            "aggregate_id": str(RUN_ID),
            "now": now,
            "traceparent": TRACE.traceparent,
            "tracestate": TRACE.tracestate,
            "correlation_id": CORRELATION_ID,
        },
    )


def _seed_transport_rows(engine: Engine) -> dict[str, object]:
    now = _database_now(engine)
    _seed_run(engine, now)
    _unwrap(PostgresJobQueue(engine).enqueue(_job(now)))
    with engine.begin() as connection:
        _insert_outbox(connection, now)
    message = InboxMessage(
        consumer="consumer-a",
        message_id="message-1",
        payload={"status": "accepted"},
        received_at=now,
        processed_at=now,
        trace_carrier=TRACE,
        correlation_id=CORRELATION_ID,
    )
    _unwrap(PostgresInbox(engine).consume(message, lambda _: {"stored": True}))
    return {"job": JOB_ID, "outbox": OUTBOX_ID, "inbox": "consumer-a"}


def _database_now(engine: Engine) -> datetime:
    with engine.connect() as connection:
        value = connection.scalar(text("select clock_timestamp()"))
    assert isinstance(value, datetime)
    return value


def _unwrap(result: object) -> object:
    assert isinstance(result, Success)
    return result.value
