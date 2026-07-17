from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from stonks_agent.adapters.postgres.models import OutboxRow, RunEventRow, WorkflowRunRow
from stonks_agent.adapters.postgres.secret_free_json import SecretPersistenceError

pytestmark = pytest.mark.postgres
RUN_ID = UUID("9d000000-0000-4000-8000-000000000001")
EVENT_ID = UUID("9d000000-0000-4000-8000-000000000002")
OUTBOX_ID = UUID("9d000000-0000-4000-8000-000000000003")


def test_secret_shaped_event_and_outbox_payload_roll_back_before_persistence(
    clean_database: Engine,
) -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    payload = {"authorization": "Bearer must-never-enter-postgres"}

    with (
        pytest.raises(SecretPersistenceError) as raised,
        Session(clean_database) as session,
    ):
        session.add(
            WorkflowRunRow(
                run_id=RUN_ID,
                run_type="research_pipeline",
                status="queued",
                as_of=now,
                policy_id="policy-v1",
                idempotency_key="secret-guard-run",
                input_hash="a" * 64,
                owner_subject="test:secret-guard",
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            RunEventRow(
                event_id=EVENT_ID,
                run_id=RUN_ID,
                sequence=1,
                event_type="run.queued",
                payload=payload,
                occurred_at=now,
                previous_hash=None,
                event_hash="b" * 64,
            )
        )
        session.add(
            OutboxRow(
                outbox_id=OUTBOX_ID,
                aggregate_type="run",
                aggregate_id=str(RUN_ID),
                sequence=1,
                topic="run.queued",
                payload=payload,
                idempotency_key="secret-guard-outbox",
                created_at=now,
                not_before=now,
                attempts=0,
                max_attempts=10,
                lease_generation=0,
            )
        )
        session.commit()

    assert "must-never-enter-postgres" not in str(raised.value)

    with Session(clean_database) as session:
        assert session.scalar(select(func.count()).select_from(WorkflowRunRow)) == 0
        assert session.scalar(select(func.count()).select_from(RunEventRow)) == 0
        assert session.scalar(select(func.count()).select_from(OutboxRow)) == 0
