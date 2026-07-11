from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Lock

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from stonks_agent.adapters.postgres.inbox import PostgresInbox
from stonks_agent.adapters.postgres.models import ProviderHealthRow
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.inbox import InboxMessage

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 1, 2, 21, tzinfo=UTC)


def message(*, value: str = "one") -> InboxMessage:
    return InboxMessage(
        consumer="snapshot-worker",
        message_id="provider-message-1",
        payload={"value": value},
        received_at=NOW,
        processed_at=NOW,
    )


def test_duplicate_inbox_message_runs_transactional_handler_once(
    clean_database: Engine,
) -> None:
    calls = 0
    calls_lock = Lock()

    def handler(session: Session) -> dict[str, object]:
        nonlocal calls
        with calls_lock:
            calls += 1
        session.add(
            ProviderHealthRow(
                provider="replay",
                capability="prices",
                market="US",
                state="available",
                observed_at=NOW,
                latency_ms=0,
                failure_count=0,
                quota_remaining=None,
                details={},
            )
        )
        return {"status": "accepted"}

    inbox = PostgresInbox(clean_database)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = tuple(executor.map(lambda _: inbox.consume(message(), handler), range(8)))

    assert all(isinstance(result, Success) for result in results)
    receipts = tuple(result.value for result in results if isinstance(result, Success))
    assert sum(not receipt.duplicate for receipt in receipts) == 1
    assert calls == 1
    with Session(clean_database) as session:
        assert session.scalar(select(func.count()).select_from(ProviderHealthRow)) == 1


def test_duplicate_key_with_different_payload_fails_closed(
    clean_database: Engine,
) -> None:
    inbox = PostgresInbox(clean_database)
    first = inbox.consume(message(), lambda _: {"status": "accepted"})
    conflict = inbox.consume(
        message(value="different"),
        lambda _: {"status": "must-not-run"},
    )

    assert isinstance(first, Success)
    assert isinstance(conflict, Failure)
    assert conflict.error.code is ErrorCode.CONFLICT
    assert conflict.error.message == "Inbox message payload conflicts with prior receipt"


def test_handler_failure_rolls_back_receipt_and_can_retry(
    clean_database: Engine,
) -> None:
    inbox = PostgresInbox(clean_database)

    def fail(_: Session) -> dict[str, object]:
        raise RuntimeError("secret internal detail")

    failed = inbox.consume(message(), fail)
    retried = inbox.consume(message(), lambda _: {"status": "recovered"})

    assert isinstance(failed, Failure)
    assert failed.error.code is ErrorCode.INTERNAL_ERROR
    assert "secret" not in failed.error.message
    assert isinstance(retried, Success)
    assert not retried.value.duplicate
    assert retried.value.result == {"status": "recovered"}
