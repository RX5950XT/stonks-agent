from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from stonks_agent.adapters.artifacts.local import LocalArtifactStore
from stonks_agent.adapters.postgres.gui_research import (
    PostgresGuiResearchFacade,
)
from stonks_agent.adapters.postgres.job_queue import PostgresJobQueue
from stonks_agent.adapters.postgres.models import JobRow, WorkflowRunRow
from stonks_agent.composition.runtime import LocalRuntime
from stonks_agent.domain.auth import LocalPrincipal, Role
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.job import JobStatus
from stonks_agent.domain.workflow import WorkflowStatus
from stonks_agent.entrypoints.api.gui_research import GuiResearchApiOptions
from stonks_contracts.common import stable_payload_hash

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
pytest_plugins = ["integration.postgres.conftest"]

NOW = datetime(2026, 7, 28, 8, tzinfo=UTC)
RUN_ID = UUID("75100000-0000-4000-8000-000000000001")
JOB_ID = UUID("75100000-0000-4000-8000-000000000002")


def test_failed_research_projects_typed_terminal_state(
    clean_database: Engine,
    tmp_path: Path,
) -> None:
    _seed_failed_research(clean_database)
    client = httpx.Client(trust_env=False)
    facade = PostgresGuiResearchFacade(
        runtime=LocalRuntime(
            engine=clean_database,
            artifacts=LocalArtifactStore(tmp_path / "artifacts"),
            http_client=client,
        ),
        queue=PostgresJobQueue(clean_database),
        handlers={},
    )
    try:
        principal = GuiResearchApiOptions().principal
        view = facade.read(principal, RUN_ID)
        events = facade.events(
            principal,
            RUN_ID,
            after_sequence=0,
            limit=100,
        )
    finally:
        client.close()

    assert isinstance(view, Success)
    assert view.value.status == "failed"
    assert view.value.error_code == ErrorCode.CONFIGURATION_INVALID.value
    assert isinstance(events, Success)
    assert tuple(item.event_type for item in events.value) == (
        "research.queued",
        "research.running",
        "research.failed",
    )


def test_research_projection_rejects_another_local_subject(
    clean_database: Engine,
    tmp_path: Path,
) -> None:
    _seed_failed_research(clean_database)
    client = httpx.Client(trust_env=False)
    facade = PostgresGuiResearchFacade(
        runtime=LocalRuntime(
            engine=clean_database,
            artifacts=LocalArtifactStore(tmp_path / "artifacts"),
            http_client=client,
        ),
        queue=PostgresJobQueue(clean_database),
        handlers={},
    )
    try:
        result = facade.read(
            LocalPrincipal(
                subject="another-local-user",
                roles=frozenset({Role.RESEARCHER}),
            ),
            RUN_ID,
        )
    finally:
        client.close()

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.FORBIDDEN


def test_history_is_owner_scoped_and_failed_run_does_not_invent_evidence(
    clean_database: Engine,
    tmp_path: Path,
) -> None:
    _seed_failed_research(clean_database)
    client = httpx.Client(trust_env=False)
    facade = PostgresGuiResearchFacade(
        runtime=LocalRuntime(
            engine=clean_database,
            artifacts=LocalArtifactStore(tmp_path / "artifacts"),
            http_client=client,
        ),
        queue=PostgresJobQueue(clean_database),
        handlers={},
    )
    principal = GuiResearchApiOptions().principal
    another = LocalPrincipal(
        subject="another-local-user",
        roles=frozenset({Role.RESEARCHER}),
    )
    try:
        history = facade.recent(principal, limit=10)
        other_history = facade.recent(another, limit=10)
        evidence = facade.evidence(principal, RUN_ID)
    finally:
        client.close()

    assert isinstance(history, Success)
    assert tuple(item.run_id for item in history.value.items) == (RUN_ID,)
    assert history.value.items[0].status == "failed"
    assert isinstance(other_history, Success)
    assert other_history.value.items == ()
    assert isinstance(evidence, Success)
    assert evidence.value.items == ()


def _seed_failed_research(engine: Engine) -> None:
    payload: dict[str, object] = {
        "instrument_id": "instrument:aapl",
        "symbol": "AAPL",
    }
    with Session(engine) as session, session.begin():
        run = WorkflowRunRow(
            run_id=RUN_ID,
            run_type="research_report",
            status=WorkflowStatus.FAILED.value,
            as_of=NOW,
            policy_id="balanced/1",
            idempotency_key="test:gui-research:failed",
            input_hash="a" * 64,
            owner_subject="local-console-research",
            version=2,
            created_at=NOW,
            updated_at=NOW + timedelta(seconds=2),
        )
        session.add(run)
        session.flush()
        session.add(
            JobRow(
                job_id=JOB_ID,
                run_id=RUN_ID,
                job_type="research_pipeline",
                payload=payload,
                payload_hash=stable_payload_hash(payload),
                status=JobStatus.DEAD_LETTER.value,
                idempotency_key="test:gui-research:failed:job",
                not_before=NOW,
                deadline_at=NOW + timedelta(minutes=30),
                attempts=1,
                max_attempts=3,
                attempt_generation=1,
                last_error={
                    "code": ErrorCode.CONFIGURATION_INVALID.value,
                    "reason": "research_configuration_invalid",
                },
                created_at=NOW,
                updated_at=NOW + timedelta(seconds=2),
            )
        )
