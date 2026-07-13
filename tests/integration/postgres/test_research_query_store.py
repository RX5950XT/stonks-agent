from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from stonks_agent.adapters.postgres.models import (
    ArtifactManifestRow,
    DatasetSnapshotRow,
    JobRow,
    RunDatasetSnapshotRow,
    RunEventRow,
    WorkflowRunRow,
)
from stonks_agent.adapters.postgres.research_query import (
    PostgresResearchRequestStore,
    PostgresRunEventReader,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.research_run import ResearchRunRequest
from stonks_agent.entrypoints.cli import app
from stonks_contracts.common import stable_payload_hash

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 7, 13, 8, tzinfo=UTC)
SNAPSHOT_ID = UUID("38000000-0000-4000-8000-000000000001")


def test_submit_atomically_links_snapshot_and_is_idempotent(
    clean_database: Engine,
) -> None:
    seed_snapshot(clean_database)
    store = PostgresResearchRequestStore(clean_database)
    command = request()

    first = store.submit(command)
    second = store.submit(
        command.model_copy(update={"requested_at": NOW + timedelta(seconds=1)})
    )

    assert isinstance(first, Success)
    assert second == first
    with Session(clean_database) as session:
        assert session.scalar(select(func.count()).select_from(WorkflowRunRow)) == 1
        assert session.scalar(select(func.count()).select_from(JobRow)) == 1
        link = session.scalar(select(RunDatasetSnapshotRow))
        assert link is not None
        assert link.run_id == first.value.run_id
        assert link.snapshot_id == SNAPSHOT_ID
        job = session.scalar(select(JobRow))
        assert job is not None
        assert job.job_type == "research_pipeline"
        assert "execution_mode" in job.payload
        assert not ({"order", "target", "risk_override"} & set(job.payload))


def test_submit_rejects_missing_or_wrong_asof_snapshot_and_identity_conflict(
    clean_database: Engine,
) -> None:
    store = PostgresResearchRequestStore(clean_database)
    missing = store.submit(request())
    seed_snapshot(clean_database)
    wrong_asof = store.submit(
        request(key="wrong-asof", as_of=NOW + timedelta(minutes=1))
    )
    accepted = store.submit(request())
    conflict = store.submit(request().model_copy(update={"symbol": "MSFT"}))

    assert isinstance(missing, Failure)
    assert missing.error.code is ErrorCode.NOT_FOUND
    assert isinstance(wrong_asof, Failure)
    assert wrong_asof.error.code is ErrorCode.CONFLICT
    assert isinstance(accepted, Success)
    assert isinstance(conflict, Failure)
    assert conflict.error.code is ErrorCode.CONFLICT


def test_event_reader_validates_full_hash_chain_before_projection(
    clean_database: Engine,
) -> None:
    seed_snapshot(clean_database)
    submitted = PostgresResearchRequestStore(clean_database).submit(request())
    assert isinstance(submitted, Success)
    payload = {"status": "degraded", "reason": "provider_unavailable"}
    event_id = uuid4()
    event_hash = stable_payload_hash(
        {
            "event_id": str(event_id),
            "sequence": 2,
            "previous_hash": None,
            "payload": payload,
        }
    )
    with Session(clean_database) as session, session.begin():
        run = session.get(WorkflowRunRow, submitted.value.run_id)
        assert run is not None
        run.version = 2
        run.updated_at = NOW + timedelta(seconds=1)
        session.add(
            RunEventRow(
                event_id=event_id,
                run_id=run.run_id,
                sequence=2,
                event_type="research.degraded",
                payload=payload,
                occurred_at=run.updated_at,
                previous_hash=None,
                event_hash=event_hash,
            )
        )

    reader = PostgresRunEventReader(clean_database)
    result = reader.list_after(submitted.value.run_id, after_sequence=0, limit=10)
    assert isinstance(result, Success)
    assert result.value[0].event_hash == event_hash

    other = PostgresResearchRequestStore(clean_database).submit(
        request(key="research-store-tampered")
    )
    assert isinstance(other, Success)
    with Session(clean_database) as session, session.begin():
        run = session.get(WorkflowRunRow, other.value.run_id)
        assert run is not None
        run.version = 2
        run.updated_at = NOW + timedelta(seconds=1)
        session.add(
            RunEventRow(
                event_id=uuid4(),
                run_id=run.run_id,
                sequence=2,
                event_type="research.degraded",
                payload=payload,
                occurred_at=run.updated_at,
                previous_hash=None,
                event_hash="f" * 64,
            )
        )
    tampered = reader.list_after(other.value.run_id, after_sequence=0, limit=10)
    assert isinstance(tampered, Failure)
    assert tampered.error.code is ErrorCode.CONFLICT


def test_research_cli_submits_through_postgres_store(
    clean_database: Engine, postgres_url: str
) -> None:
    seed_snapshot(clean_database, created_at=datetime.now(UTC) - timedelta(seconds=1))

    result = CliRunner().invoke(
        app,
        [
            "research",
            "request",
            "--as-of",
            NOW.isoformat(),
            "--snapshot-id",
            str(SNAPSHOT_ID),
            "--idempotency-key",
            "research-cli-postgres",
        ],
        env={"STONKS_DATABASE_URL": postgres_url},
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["status"] == 202
    with Session(clean_database) as session:
        job = session.scalar(
            select(JobRow).where(
                JobRow.idempotency_key == "research:research-cli-postgres:job"
            )
        )
        assert job is not None
        assert job.job_type == "research_pipeline"


def request(
    *, key: str = "research-store-1", as_of: datetime = NOW
) -> ResearchRunRequest:
    return ResearchRunRequest(
        instrument_id="instrument-aapl",
        symbol="AAPL",
        as_of=as_of,
        snapshot_id=SNAPSHOT_ID,
        research_profile_id="balanced/1",
        model_policy_id="research-models/1",
        language="zh-TW",
        idempotency_key=key,
        requested_at=NOW,
    )


def seed_snapshot(engine: Engine, *, created_at: datetime = NOW) -> None:
    artifact_hash = "a" * 64
    with Session(engine) as session, session.begin():
        session.add(
            ArtifactManifestRow(
                content_hash=artifact_hash,
                size_bytes=2,
                media_type="application/json",
                license_tag="Apache-2.0",
                sensitivity="internal",
                source="test",
                finalized_at=NOW - timedelta(seconds=1),
                storage_uri=f"artifact://sha256/{artifact_hash}",
                metadata_payload={},
            )
        )
        session.add(
            DatasetSnapshotRow(
                snapshot_id=SNAPSHOT_ID,
                as_of=NOW,
                cutoff_at=NOW,
                provider_policy_id="canonical/1",
                manifest_artifact_hash=artifact_hash,
                content_hash="b" * 64,
                manifest={},
                created_at=created_at,
            )
        )
