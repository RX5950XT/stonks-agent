from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from stonks_agent.adapters.postgres.outbox import PostgresOutbox
from stonks_agent.domain.errors import ErrorCode, Failure, Success

NOW = datetime(2026, 1, 2, 21, tzinfo=UTC)
OUTBOX_ID = UUID("50000000-0000-4000-8000-000000000001")


def test_skip_locked_outbox_claim_has_single_owner(clean_database: Engine) -> None:
    with clean_database.begin() as connection:
        insert_outbox(connection)
    outbox = PostgresOutbox(clean_database)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(
                lambda worker: outbox.claim(
                    worker_id=worker,
                    now=NOW,
                    lease_for=timedelta(seconds=30),
                    limit=1,
                ),
                ("publisher-a", "publisher-b"),
            )
        )

    claimed = [result.value for result in results if isinstance(result, Success)]
    assert sorted(len(batch) for batch in claimed) == [0, 1]
    nonempty = next(batch for batch in claimed if batch)
    assert nonempty[0].outbox_id == OUTBOX_ID


def test_nack_schedules_retry_then_ack_is_idempotent(clean_database: Engine) -> None:
    with clean_database.begin() as connection:
        insert_outbox(connection)
    outbox = PostgresOutbox(clean_database)
    first = unwrap(
        outbox.claim(
            worker_id="publisher-a",
            now=NOW,
            lease_for=timedelta(seconds=30),
            limit=1,
        )
    )[0]

    nacked = outbox.nack(
        OUTBOX_ID,
        worker_id="publisher-a",
        now=NOW + timedelta(seconds=1),
        retry_at=NOW + timedelta(seconds=10),
        error_code="provider_unavailable",
    )
    early = unwrap(
        outbox.claim(
            worker_id="publisher-b",
            now=NOW + timedelta(seconds=5),
            lease_for=timedelta(seconds=30),
            limit=1,
        )
    )
    retried = unwrap(
        outbox.claim(
            worker_id="publisher-b",
            now=NOW + timedelta(seconds=10),
            lease_for=timedelta(seconds=30),
            limit=1,
        )
    )[0]
    acked = outbox.ack(
        OUTBOX_ID,
        worker_id="publisher-b",
        now=NOW + timedelta(seconds=11),
    )
    duplicate_ack = outbox.ack(
        OUTBOX_ID,
        worker_id="publisher-b",
        now=NOW + timedelta(seconds=12),
    )

    assert isinstance(nacked, Success)
    assert early == ()
    assert retried.attempts == first.attempts + 1
    assert isinstance(acked, Success)
    assert isinstance(duplicate_ack, Success)
    assert duplicate_ack.value == acked.value


def test_wrong_owner_cannot_ack_outbox(clean_database: Engine) -> None:
    with clean_database.begin() as connection:
        insert_outbox(connection)
    outbox = PostgresOutbox(clean_database)
    unwrap(
        outbox.claim(
            worker_id="publisher-a",
            now=NOW,
            lease_for=timedelta(seconds=30),
            limit=1,
        )
    )

    result = outbox.ack(
        OUTBOX_ID,
        worker_id="publisher-b",
        now=NOW + timedelta(seconds=1),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT


def insert_outbox(connection: Connection) -> None:
    connection.execute(
        text(
            """
            insert into outbox
                (outbox_id, aggregate_type, aggregate_id, sequence, topic,
                 payload, idempotency_key, created_at, not_before, attempts)
            values
                (:id, 'run', 'run-1', 1, 'test.event', '{}'::jsonb,
                 'outbox-idempotency', :now, :now, 0)
            """
        ),
        {"id": OUTBOX_ID, "now": NOW},
    )


def unwrap(result: object) -> object:
    assert isinstance(result, Success)
    return result.value
