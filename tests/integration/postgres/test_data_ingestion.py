from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, text
from typer.testing import CliRunner

from stonks_agent.adapters.postgres.snapshot_requests import (
    PostgresSnapshotRequestStore,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.snapshot import CreateSnapshotRequest
from stonks_agent.entrypoints.cli import app

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 1, 2, 21, tzinfo=UTC)


def request(*, query: dict[str, object]) -> CreateSnapshotRequest:
    return CreateSnapshotRequest(
        market="US",
        capability="prices",
        as_of=NOW,
        query=query,
        provider_policy_id="us-prices/1",
        idempotency_key="snapshot-idempotency",
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
    assert isinstance(conflict, Failure)
    assert conflict.error.code is ErrorCode.CONFLICT
    with clean_database.connect() as connection:
        counts = connection.execute(
            text("select (select count(*) from run), (select count(*) from job)")
        ).one()
    assert counts == (1, 1)


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
        env={"STONKS_DATABASE_URL": postgres_url},
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
