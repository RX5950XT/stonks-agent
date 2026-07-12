from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Connection, Engine, text

from stonks_agent.adapters.postgres.outbox import PostgresOutbox
from stonks_agent.domain.errors import ErrorCode, Failure, Success

OUTBOX_ID = UUID("50000000-0000-4000-8000-000000000001")
pytestmark = pytest.mark.postgres


def test_skip_locked_outbox_claim_has_single_owner(clean_database: Engine) -> None:
    now = database_now(clean_database)
    with clean_database.begin() as connection:
        insert_outbox(connection, now=now)
    outbox = PostgresOutbox(clean_database)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(
                lambda worker: outbox.claim(
                    worker_id=worker,
                    now=now,
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
    now = database_now(clean_database)
    with clean_database.begin() as connection:
        insert_outbox(connection, now=now)
    outbox = PostgresOutbox(clean_database)
    first = unwrap(
        outbox.claim(
            worker_id="publisher-a",
            now=now,
            lease_for=timedelta(seconds=30),
            limit=1,
        )
    )[0]

    nacked = outbox.nack(
        OUTBOX_ID,
        worker_id="publisher-a",
        lease_generation=first.lease_generation,
        lease_nonce=first.lease_nonce,
        now=now,
        retry_at=now + timedelta(minutes=10),
        error_code="provider_unavailable",
    )
    early = unwrap(
        outbox.claim(
            worker_id="publisher-b",
            now=now,
            lease_for=timedelta(seconds=30),
            limit=1,
        )
    )
    with clean_database.begin() as connection:
        connection.execute(
            text("update outbox set not_before = :due where outbox_id = :id"),
            {"due": now - timedelta(seconds=1), "id": OUTBOX_ID},
        )
    retried = unwrap(
        outbox.claim(
            worker_id="publisher-b",
            now=now,
            lease_for=timedelta(seconds=30),
            limit=1,
        )
    )[0]
    acked = outbox.ack(
        OUTBOX_ID,
        worker_id="publisher-b",
        lease_generation=retried.lease_generation,
        lease_nonce=retried.lease_nonce,
        now=now,
    )
    duplicate_ack = outbox.ack(
        OUTBOX_ID,
        worker_id="publisher-b",
        lease_generation=retried.lease_generation,
        lease_nonce=retried.lease_nonce,
        now=now,
    )

    assert isinstance(nacked, Success)
    assert early == ()
    assert retried.attempts == first.attempts + 1
    assert isinstance(acked, Success)
    assert isinstance(duplicate_ack, Success)
    assert duplicate_ack.value == acked.value


def test_wrong_owner_cannot_ack_outbox(clean_database: Engine) -> None:
    now = database_now(clean_database)
    with clean_database.begin() as connection:
        insert_outbox(connection, now=now)
    outbox = PostgresOutbox(clean_database)
    lease = unwrap(
        outbox.claim(
            worker_id="publisher-a",
            now=now,
            lease_for=timedelta(seconds=30),
            limit=1,
        )
    )

    result = outbox.ack(
        OUTBOX_ID,
        worker_id="publisher-b",
        lease_generation=lease[0].lease_generation,
        lease_nonce=lease[0].lease_nonce,
        now=now,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT


def test_same_owner_stale_generation_cannot_ack_reclaimed_outbox(
    clean_database: Engine,
) -> None:
    now = database_now(clean_database)
    with clean_database.begin() as connection:
        insert_outbox(connection, now=now)
    outbox = PostgresOutbox(clean_database)
    first = unwrap(
        outbox.claim(
            worker_id="publisher-a",
            now=now,
            lease_for=timedelta(seconds=5),
            limit=1,
        )
    )[0]
    with clean_database.begin() as connection:
        connection.execute(
            text("update outbox set lease_until = :expired where outbox_id = :id"),
            {"expired": now - timedelta(seconds=1), "id": OUTBOX_ID},
        )
    reclaimed = unwrap(
        outbox.claim(
            worker_id="publisher-a",
            now=now,
            lease_for=timedelta(seconds=30),
            limit=1,
        )
    )[0]

    stale = outbox.ack(
        OUTBOX_ID,
        worker_id="publisher-a",
        lease_generation=first.lease_generation,
        lease_nonce=first.lease_nonce,
        now=now,
    )
    current = outbox.ack(
        OUTBOX_ID,
        worker_id="publisher-a",
        lease_generation=reclaimed.lease_generation,
        lease_nonce=reclaimed.lease_nonce,
        now=now,
    )

    assert reclaimed.lease_generation == first.lease_generation + 1
    assert reclaimed.lease_nonce != first.lease_nonce
    assert isinstance(stale, Failure)
    assert stale.error.code is ErrorCode.CONFLICT
    assert isinstance(current, Success)


def test_same_owner_stale_generation_cannot_nack_reclaimed_outbox(
    clean_database: Engine,
) -> None:
    now = database_now(clean_database)
    with clean_database.begin() as connection:
        insert_outbox(connection, now=now)
    outbox = PostgresOutbox(clean_database)
    first = unwrap(
        outbox.claim(
            worker_id="publisher-a",
            now=now,
            lease_for=timedelta(seconds=5),
            limit=1,
        )
    )[0]
    with clean_database.begin() as connection:
        connection.execute(
            text("update outbox set lease_until = :expired where outbox_id = :id"),
            {"expired": now - timedelta(seconds=1), "id": OUTBOX_ID},
        )
    reclaimed = unwrap(
        outbox.claim(
            worker_id="publisher-a",
            now=now,
            lease_for=timedelta(seconds=30),
            limit=1,
        )
    )[0]

    stale = outbox.nack(
        OUTBOX_ID,
        worker_id="publisher-a",
        lease_generation=first.lease_generation,
        lease_nonce=first.lease_nonce,
        now=now,
        retry_at=now + timedelta(minutes=10),
        error_code="stale_attempt",
    )
    current = outbox.nack(
        OUTBOX_ID,
        worker_id="publisher-a",
        lease_generation=reclaimed.lease_generation,
        lease_nonce=reclaimed.lease_nonce,
        now=now,
        retry_at=now + timedelta(minutes=10),
        error_code="provider_unavailable",
    )

    assert isinstance(stale, Failure)
    assert stale.error.code is ErrorCode.CONFLICT
    assert isinstance(current, Success)


def test_future_caller_cannot_claim_outbox_before_database_not_before(
    clean_database: Engine,
) -> None:
    anchor = database_now(clean_database)
    with clean_database.begin() as connection:
        insert_outbox(
            connection,
            now=anchor,
            not_before=anchor + timedelta(minutes=10),
        )

    result = PostgresOutbox(clean_database).claim(
        worker_id="future-publisher",
        now=anchor + timedelta(minutes=20),
        lease_for=timedelta(minutes=1),
        limit=1,
    )

    assert isinstance(result, Success)
    assert result.value == ()


@pytest.mark.parametrize("operation", ("ack", "nack"))
def test_stale_caller_cannot_mutate_database_expired_outbox_lease(
    clean_database: Engine,
    operation: str,
) -> None:
    anchor = database_now(clean_database)
    with clean_database.begin() as connection:
        insert_outbox(connection, now=anchor)
    outbox = PostgresOutbox(clean_database)
    lease = unwrap(
        outbox.claim(
            worker_id="publisher-a",
            now=anchor,
            lease_for=timedelta(minutes=5),
            limit=1,
        )
    )[0]
    with clean_database.begin() as connection:
        connection.execute(
            text("update outbox set lease_until = :expired where outbox_id = :id"),
            {"expired": anchor - timedelta(seconds=1), "id": OUTBOX_ID},
        )

    if operation == "ack":
        result = outbox.ack(
            OUTBOX_ID,
            worker_id="publisher-a",
            lease_generation=lease.lease_generation,
            lease_nonce=lease.lease_nonce,
            now=anchor - timedelta(days=1),
        )
    else:
        result = outbox.nack(
            OUTBOX_ID,
            worker_id="publisher-a",
            lease_generation=lease.lease_generation,
            lease_nonce=lease.lease_nonce,
            now=anchor - timedelta(days=1),
            retry_at=anchor + timedelta(minutes=1),
            error_code="temporary_failure",
        )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    with clean_database.connect() as connection:
        state = connection.execute(
            text("select published_at, not_before from outbox where outbox_id = :id"),
            {"id": OUTBOX_ID},
        ).one()
    assert state == (None, anchor)


def test_nack_retry_must_be_after_database_time_not_stale_caller_time(
    clean_database: Engine,
) -> None:
    anchor = database_now(clean_database)
    with clean_database.begin() as connection:
        insert_outbox(connection, now=anchor)
    outbox = PostgresOutbox(clean_database)
    lease = unwrap(
        outbox.claim(
            worker_id="publisher-a",
            now=anchor,
            lease_for=timedelta(minutes=5),
            limit=1,
        )
    )[0]

    result = outbox.nack(
        OUTBOX_ID,
        worker_id="publisher-a",
        lease_generation=lease.lease_generation,
        lease_nonce=lease.lease_nonce,
        now=anchor - timedelta(days=1),
        retry_at=anchor - timedelta(seconds=1),
        error_code="temporary_failure",
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.INVALID_INPUT


def test_ack_uses_database_timestamp_despite_future_caller_time(
    clean_database: Engine,
) -> None:
    anchor = database_now(clean_database)
    with clean_database.begin() as connection:
        insert_outbox(connection, now=anchor)
    outbox = PostgresOutbox(clean_database)
    lease = unwrap(
        outbox.claim(
            worker_id="publisher-a",
            now=anchor,
            lease_for=timedelta(minutes=5),
            limit=1,
        )
    )[0]

    result = outbox.ack(
        OUTBOX_ID,
        worker_id="publisher-a",
        lease_generation=lease.lease_generation,
        lease_nonce=lease.lease_nonce,
        now=anchor + timedelta(days=1),
    )

    assert isinstance(result, Success)
    assert anchor < lease.lease_until < anchor + timedelta(minutes=6)
    assert anchor <= result.value.published_at < anchor + timedelta(minutes=1)


def insert_outbox(
    connection: Connection,
    *,
    now: datetime,
    not_before: datetime | None = None,
) -> None:
    connection.execute(
        text(
            """
            insert into outbox
                (outbox_id, aggregate_type, aggregate_id, sequence, topic,
                 payload, idempotency_key, created_at, not_before, attempts)
            values
                (:id, 'run', 'run-1', 1, 'test.event', '{}'::jsonb,
                 'outbox-idempotency', :now, :not_before, 0)
            """
        ),
        {"id": OUTBOX_ID, "now": now, "not_before": not_before or now},
    )


def unwrap(result: object) -> object:
    assert isinstance(result, Success)
    return result.value


def database_now(engine: Engine) -> datetime:
    with engine.connect() as connection:
        value = connection.scalar(text("select clock_timestamp()"))
    assert isinstance(value, datetime)
    return value
