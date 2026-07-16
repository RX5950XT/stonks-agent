from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, text
from typer.testing import CliRunner

from stonks_agent.adapters.postgres.snapshot_requests import (
    PostgresSnapshotRequestStore,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.snapshot import CreateSnapshotRequest
from stonks_agent.entrypoints.cli import app
from stonks_contracts.common import stable_payload_hash

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 1, 2, 21, tzinfo=UTC)


def request(
    *,
    query: dict[str, object],
    owner_subject: str = "test-owner",
) -> CreateSnapshotRequest:
    return CreateSnapshotRequest(
        market="US",
        capability="prices",
        as_of=NOW,
        query=query,
        provider_policy_id="us-prices/1",
        idempotency_key="snapshot-idempotency",
        owner_subject=owner_subject,
        requested_at=NOW,
    )


def test_snapshot_run_and_job_are_atomic_and_idempotent(clean_database: Engine) -> None:
    store = PostgresSnapshotRequestStore(clean_database)

    first = store.submit(request(query={"symbol": "AAPL"}))
    same = store.submit(request(query={"symbol": "AAPL"}))
    conflict = store.submit(request(query={"symbol": "MSFT"}))

    assert isinstance(first, Success)
    assert isinstance(same, Success)
    assert same.value == first.value
    assert first.value.snapshot_id is None
    assert isinstance(conflict, Failure)
    assert conflict.error.code is ErrorCode.CONFLICT
    with clean_database.connect() as connection:
        counts = connection.execute(
            text("select (select count(*) from run), (select count(*) from job)")
        ).one()
    assert counts == (1, 1)
    with clean_database.connect() as connection:
        payload = connection.scalar(text("select payload from job"))
    assert isinstance(payload, dict)
    assert "snapshot_id" not in payload


def test_snapshot_idempotency_is_scoped_to_owner(clean_database: Engine) -> None:
    store = PostgresSnapshotRequestStore(clean_database)

    first = store.submit(
        request(query={"symbol": "AAPL"}, owner_subject="researcher:alice")
    )
    second = store.submit(
        request(query={"symbol": "AAPL"}, owner_subject="researcher:bob")
    )

    assert isinstance(first, Success)
    assert isinstance(second, Success)
    assert first.value.run_id != second.value.run_id
    assert first.value.job_id != second.value.job_id
    with clean_database.connect() as connection:
        records = connection.execute(
            text(
                "select r.run_id, r.owner_subject, j.job_id "
                "from run r join job j using (run_id) order by r.owner_subject"
            )
        ).all()
    rows = [(row.run_id, row.owner_subject, row.job_id) for row in records]
    assert rows == [
        (first.value.run_id, "researcher:alice", first.value.job_id),
        (second.value.run_id, "researcher:bob", second.value.job_id),
    ]


def test_snapshot_retry_rejects_request_timestamp_drift(
    clean_database: Engine,
) -> None:
    store = PostgresSnapshotRequestStore(clean_database)
    original = request(query={"symbol": "AAPL"})

    first = store.submit(original)
    drifted = store.submit(
        original.model_copy(update={"requested_at": NOW + timedelta(seconds=1)})
    )

    assert isinstance(first, Success)
    assert isinstance(drifted, Failure)
    assert drifted.error.code is ErrorCode.CONFLICT
    assert _run_job_counts(clean_database) == (1, 1)


@pytest.mark.parametrize(
    "tamper_sql",
    [
        "update run set run_type = 'rogue'",
        "update run set as_of = as_of + interval '1 day'",
        "update run set policy_id = 'rogue-policy/1'",
        "update run set idempotency_key = 'rogue'",
        "update run set input_hash = repeat('a', 64)",
        "update run set created_at = created_at + interval '1 second'",
        "update job set job_id = '71000000-0000-4000-8000-000000000001'",
        "update job set job_type = 'rogue'",
        "update job set payload = jsonb_set(payload, '{query,symbol}', '\"MSFT\"')",
        "update job set payload_hash = repeat('b', 64)",
        "update job set idempotency_key = 'rogue'",
        "update job set not_before = not_before + interval '1 second'",
        "update job set deadline_at = deadline_at + interval '1 second'",
        "update job set max_attempts = max_attempts + 1",
        "update job set created_at = created_at + interval '1 second'",
    ],
)
def test_snapshot_retry_revalidates_full_immutable_run_job_identity(
    clean_database: Engine,
    tamper_sql: str,
) -> None:
    store = PostgresSnapshotRequestStore(clean_database)
    original = request(query={"symbol": "AAPL"})
    assert isinstance(store.submit(original), Success)
    with clean_database.begin() as connection:
        connection.execute(text(tamper_sql))

    result = store.submit(original)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    assert _run_job_counts(clean_database) == (1, 1)


def test_snapshot_retry_rejects_consistently_rehashed_job_payload_drift(
    clean_database: Engine,
) -> None:
    store = PostgresSnapshotRequestStore(clean_database)
    original = request(query={"symbol": "AAPL"})
    assert isinstance(store.submit(original), Success)
    drifted_payload = original.model_dump(mode="json")
    drifted_payload["query"] = {"symbol": "MSFT"}
    with clean_database.begin() as connection:
        connection.execute(
            text(
                "update job set payload = jsonb_set(payload, "
                "'{query,symbol}', '\"MSFT\"'), payload_hash = :payload_hash"
            ),
            {"payload_hash": stable_payload_hash(drifted_payload)},
        )

    result = store.submit(original)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    assert _run_job_counts(clean_database) == (1, 1)


def _run_job_counts(engine: Engine) -> tuple[int, int]:
    with engine.connect() as connection:
        row = connection.execute(
            text("select (select count(*) from run), (select count(*) from job)")
        ).one()
    return int(row[0]), int(row[1])


def test_data_cli_enqueues_and_returns_refs(
    clean_database: Engine,
    postgres_url: str,
) -> None:
    del clean_database
    result = CliRunner().invoke(
        app,
        [
            "data",
            "request-snapshot",
            "--market",
            "US",
            "--capability",
            "prices",
            "--as-of",
            NOW.isoformat(),
            "--query-json",
            '{"symbol":"AAPL"}',
            "--provider-policy-id",
            "us-prices/1",
            "--idempotency-key",
            "cli-snapshot-1",
        ],
        env={
            "STONKS_DATABASE_URL": postgres_url,
            "STONKS_ENVIRONMENT": "test",
        },
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert set(payload["data"]) == {
        "run_id",
        "job_id",
        "snapshot_id",
        "evidence_refs",
    }
